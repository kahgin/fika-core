from __future__ import annotations

import json
import math
import uuid
from datetime import date, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.services.vrp_model import vrp_config
from app.services.vrp_utils import build_problem
from app.services.or_tools_cvrptw import run_cvrptw
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.city_day_allocator import allocate_days_to_cities
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Performance guardrails
MAX_CITIES_PER_REQUEST = 2
MAX_POIS_PER_CITY = 300


def _log_event(event: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    """Emit structured JSON log with event name and request_id."""
    log_data = {"event": event, **payload}
    if request_id:
        log_data["request_id"] = request_id
    logger.info(json.dumps(log_data))


def segment_by_city(maut_output: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """
    Segment POIs by city using hierarchical fallback strategy.

    1. poi.get("area_name")
    2. KMeans clustering on (lat, lng) coordinates (fallback)

    Args:
        maut_output: Full MAUT output with places and meta
        request_id: Optional request ID for logging

    Returns:
        Dict mapping area_name -> maut_suboutput with filtered places and updated meta
    """
    places = maut_output.get("places", [])
    if not places:
        logger.warning("segment_by_city: No places in MAUT output")
        return {}

    def _normalize_area_name(raw: Optional[str]) -> Optional[str]:
        """Normalize area name to handle variants like 'Johor' vs 'Johor, Malaysia'."""
        if not raw:
            return None
        name = str(raw).strip()
        # Remove country suffix (e.g., "Johor, Malaysia" -> "Johor")
        if "," in name:
            name = name.split(",")[0].strip()
        return name

    # Group POIs by city
    city_groups: Dict[str, List[Dict]] = {}
    uncategorized: List[Dict] = []

    for poi in places:
        area_name = None

        # Area_name (normalized)
        if not area_name and poi.get("area_name"):
            area_name = _normalize_area_name(poi["area_name"])

        if area_name:
            city_groups.setdefault(area_name, []).append(poi)
        else:
            uncategorized.append(poi)

    # Fallback: KMeans clustering for uncategorized POIs
    if uncategorized:
        coords_list = []
        valid_pois = []

        for poi in uncategorized:
            coords = poi.get("coordinates", {})
            lat = coords.get("lat")
            lng = coords.get("lng")

            if lat is not None and lng is not None and math.isfinite(lat) and math.isfinite(lng):
                coords_list.append([lat, lng])
                valid_pois.append(poi)
            else:
                logger.warning(f"segment_by_city: POI {poi.get('id', 'unknown')} has invalid coordinates, skipping")

        if coords_list:
            try:
                from sklearn.cluster import KMeans
                import numpy as np

                # Use elbow method to determine optimal k (simplified: use sqrt(n/2))
                n = len(coords_list)
                k = max(1, min(int(math.sqrt(n / 2)), 5))  # Cap at 5 clusters

                if k == 1:
                    city_groups.setdefault("cluster_0", []).extend(valid_pois)
                else:
                    coords_array = np.array(coords_list)
                    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
                    labels = kmeans.fit_predict(coords_array)

                    for idx, label in enumerate(labels):
                        cluster_name = f"cluster_{label}"
                        city_groups.setdefault(cluster_name, []).append(valid_pois[idx])
            except ImportError:
                logger.warning("sklearn not available, grouping uncategorized POIs as single cluster")
                city_groups.setdefault("cluster_0", []).extend(valid_pois)

    # Build city suboutputs
    result: Dict[str, Dict[str, Any]] = {}
    meta = maut_output.get("meta", {})

    for area_name, city_pois in city_groups.items():
        # Normalize area_name on all POIs in this group to ensure consistency
        for poi in city_pois:
            poi["area_name"] = area_name

        # Create city-specific meta
        city_meta = meta.copy()
        city_meta["area_name"] = area_name

        result[area_name] = {"places": city_pois, "meta": city_meta}

        # _log_event(
        #     "segment_by_city",
        #     {
        #         "area_name": area_name,
        #         "poi_count": len(city_pois),
        #         "request_id": request_id,
        #     },
        # )

    # Assertion: at least one city identified
    if not result:
        logger.error("segment_by_city: No cities identified from MAUT output")
        raise ValueError("segment_by_city: No cities identified from MAUT output")

    return result


def _find_global_fallback_hotel(
    maut_output: Dict[str, Any],
    request_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find a global fallback hotel from all accommodations in the MAUT output.

    This is used when a city/cluster has no accommodations of its own.
    Returns the highest-scored accommodation from the entire POI list.

    Args:
        maut_output: Full MAUT output with places
        request_id: Optional request ID for logging

    Returns:
        Hotel dict with id, name, lat, lon, source, or None if no accommodations found
    """
    places = maut_output.get("places", [])

    # Find all accommodations across all POIs
    all_accommodations = [poi for poi in places if "accommodation" in poi.get("roles", [])]

    if not all_accommodations:
        return None

    # Select highest scored accommodation
    best_hotel = max(all_accommodations, key=lambda x: x.get("_score", 0))

    coords = best_hotel.get("coordinates", {})
    lat = coords.get("lat")
    lon = coords.get("lng")

    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None

    hotel = {
        "id": best_hotel["id"],
        "name": best_hotel["name"],
        "lat": lat,
        "lon": lon,
        "source": "global_fallback",
    }

    # _log_event(
    #     "global_fallback_hotel_found",
    #     {
    #         "hotel_id": hotel["id"],
    #         "hotel_name": hotel["name"],
    #         "coords": {"lat": lat, "lng": lon},
    #     },
    #     request_id,
    # )

    return hotel


def _find_nearest_accommodation(
    city_places: List[Dict[str, Any]],
    all_accommodations: List[Dict[str, Any]],
    request_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the nearest accommodation to a city's POI centroid.

    Args:
        city_places: POIs in the city/cluster
        all_accommodations: All accommodations from the full MAUT output
        request_id: Optional request ID for logging

    Returns:
        Hotel dict with id, name, lat, lon, source, or None if no valid accommodation found
    """
    if not city_places or not all_accommodations:
        return None

    # Calculate centroid of city POIs
    lat_sum = 0.0
    lon_sum = 0.0
    n = 0
    for poi in city_places:
        coords = poi.get("coordinates") or {}
        lat = coords.get("lat")
        lon = coords.get("lng")
        if lat is not None and lon is not None:
            lat_sum += float(lat)
            lon_sum += float(lon)
            n += 1

    if n == 0:
        return None

    centroid_lat = lat_sum / n
    centroid_lon = lon_sum / n

    # Find nearest accommodation to centroid
    best_hotel = None
    best_distance = float("inf")

    for poi in all_accommodations:
        coords = poi.get("coordinates") or {}
        lat = coords.get("lat")
        lon = coords.get("lng")
        if lat is None or lon is None:
            continue

        # Simple Euclidean distance (sufficient for nearby comparisons)
        dist = (float(lat) - centroid_lat) ** 2 + (float(lon) - centroid_lon) ** 2
        if dist < best_distance:
            best_distance = dist
            best_hotel = poi

    if best_hotel is None:
        return None

    coords = best_hotel.get("coordinates", {})
    lat = coords.get("lat")
    lon = coords.get("lng")

    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None

    hotel = {
        "id": best_hotel["id"],
        "name": best_hotel["name"],
        "lat": lat,
        "lon": lon,
        "source": "nearest_accommodation",
    }

    return hotel


def select_hotel_for_city(
    maut_suboutput: Dict[str, Any],
    days_for_city: int,
    user_hotels: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    global_fallback_hotel: Optional[Dict[str, Any]] = None,
    all_accommodations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Select or infer hotel for a city.

    Priority:
    1. User-provided hotel(s) for city
    2. Highest MAUT-scored accommodation in city
    3. Nearest accommodation from global list (for clusters without accommodations)
    4. Global fallback hotel (highest scored across all cities)
    5. Error if no accommodation available

    Args:
        maut_suboutput: City-specific MAUT output
        days_for_city: Number of days allocated to this city
        user_hotels: Optional user-provided hotels by city
        request_id: Optional request ID for logging
        global_fallback_hotel: Optional pre-computed global fallback hotel
        all_accommodations: Optional list of all accommodations for nearest search

    Returns:
        Hotel dict with id, name, lat, lon, source
        OR error sentinel {"status": "error", "error": "no_accommodation"|"invalid_hotel_coords"}
    """
    meta = maut_suboutput.get("meta", {})
    area_name = meta.get("area_name", "unknown")
    places = maut_suboutput.get("places", [])

    # Case 1: User-provided hotel
    if user_hotels and area_name in user_hotels:
        user_hotel = user_hotels[area_name]

        # Validate coordinates
        lat = user_hotel.get("lat")
        lon = user_hotel.get("lon") or user_hotel.get("lng")

        if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
            _log_event(
                "hotel_selected",
                {
                    "area_name": area_name,
                    "status": "error",
                    "error": "invalid_hotel_coords",
                },
                request_id,
            )
            return {"status": "error", "error": "invalid_hotel_coords"}

        hotel = {
            "id": user_hotel.get("id", f"user_hotel_{area_name}"),
            "name": user_hotel.get("name", "User Hotel"),
            "lat": lat,
            "lon": lon,
            "source": "user",
        }

        # _log_event(
        #     "hotel_selected",
        #     {
        #         "area_name": area_name,
        #         "hotel_id": hotel["id"],
        #         "hotel_name": hotel["name"],
        #         "source": "user",
        #         "coords": {"lat": lat, "lng": lon},
        #     },
        #     request_id,
        # )

        return hotel

    # Case 2: Check meta.selected_hotel first
    selected_hotel = meta.get("selected_hotel")
    if selected_hotel:
        coords = selected_hotel.get("coordinates", {})
        lat = coords.get("lat")
        lon = coords.get("lng")

        if lat is not None and lon is not None and math.isfinite(lat) and math.isfinite(lon):
            hotel = {
                "id": selected_hotel["id"],
                "name": selected_hotel["name"],
                "lat": lat,
                "lon": lon,
                "source": "maut_selected",
            }

            _log_event(
                "case_2_hotel_selected",
                {
                    "area_name": area_name,
                    "hotel_id": hotel["id"],
                    "hotel_name": hotel["name"],
                    "source": "maut_selected",
                    "coords": {"lat": lat, "lng": lon},
                },
                request_id,
            )

            return hotel

    # Case 3: Select from city's MAUT accommodations
    accommodations = [poi for poi in places if "accommodation" in poi.get("roles", [])]

    if accommodations:
        # Select highest MAUT score from city's accommodations
        best_hotel = max(accommodations, key=lambda x: x.get("_score", 0))

        coords = best_hotel.get("coordinates", {})
        lat = coords.get("lat")
        lon = coords.get("lng")

        if lat is not None and lon is not None and math.isfinite(lat) and math.isfinite(lon):
            hotel = {
                "id": best_hotel["id"],
                "name": best_hotel["name"],
                "lat": lat,
                "lon": lon,
                "source": "maut",
            }

            _log_event(
                "case_3_hotel_selected",
                {
                    "area_name": area_name,
                    "hotel_id": hotel["id"],
                    "hotel_name": hotel["name"],
                    "source": "maut",
                    "coords": {"lat": lat, "lng": lon},
                },
                request_id,
            )

            return hotel

    # Case 4: No accommodations in city - find nearest from global list
    if all_accommodations:
        nearest = _find_nearest_accommodation(places, all_accommodations, request_id)
        if nearest:
            _log_event(
                "case_4_hotel_selected",
                {
                    "area_name": area_name,
                    "hotel_id": nearest["id"],
                    "hotel_name": nearest["name"],
                    "source": "nearest_accommodation",
                    "coords": {"lat": nearest["lat"], "lng": nearest["lon"]},
                },
                request_id,
            )
            return nearest

    # Case 5: Use global fallback hotel
    if global_fallback_hotel:
        _log_event(
            "case_5_hotel_selected",
            {
                "area_name": area_name,
                "hotel_id": global_fallback_hotel["id"],
                "hotel_name": global_fallback_hotel["name"],
                "source": "global_fallback",
                "coords": {
                    "lat": global_fallback_hotel["lat"],
                    "lng": global_fallback_hotel["lon"],
                },
            },
            request_id,
        )
        return global_fallback_hotel.copy()

    # Case 6: No accommodation found anywhere - return error
    _log_event(
        "hotel_selected",
        {"area_name": area_name, "status": "error", "error": "no_accommodation"},
        request_id,
    )
    return {"status": "error", "error": "no_accommodation"}


def validate_global_rules(
    result: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate constraints on the final result.

    Checks:
    1. Meal rules: per day meals_count <= meals_max
    2. Theme concentration: warn if >4 attractions with same theme per day
    3. Mandatory POIs: missed mandatory appear in meta["missed_mandatory"]
    4. Hotel events: check-in/check-out pairing

    Args:
        result: Pipeline result with days
        config: Optional config with meals_max, etc.
        request_id: Optional request ID for logging

    Returns:
        {"ok": bool, "errors": [...], "warnings": [...]}
    """
    config = config or {}
    meals_max = config.get("meals_max", 3)
    errors: List[str] = []
    warnings: List[str] = []

    days = result.get("days", [])

    # Track hotel events for pairing validation
    checkins: Dict[str, int] = {}
    checkouts: Dict[str, int] = {}

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])

        # Check 1: Meal count
        meal_count = sum(1 for s in stops if s.get("role") == "meal")
        if meal_count > meals_max:
            errors.append(f"Day {day_idx + 1}: {meal_count} meals exceeds max {meals_max}")

        # Check 2: Theme concentration (soft - warning only, threshold is 2)
        theme_counts: Dict[str, int] = {}
        for stop in stops:
            if stop.get("role") == "attraction":
                themes = stop.get("themes", [])
                if themes:
                    primary_theme = themes[0]
                    theme_counts[primary_theme] = theme_counts.get(primary_theme, 0) + 1

        for theme, count in theme_counts.items():
            if count > 2:
                warnings.append(f"Day {day_idx + 1}: High concentration of '{theme}' theme ({count} attractions)")

        # Track hotel events
        for stop in stops:
            if stop.get("role") == "accommodation":
                hotel_id = stop.get("poi_id", "").rsplit("_", 1)[0]
                event_type = stop.get("hotel_event_type")
                if event_type == "checkin":
                    checkins[hotel_id] = day_idx + 1
                elif event_type == "checkout":
                    checkouts[hotel_id] = day_idx + 1

    # Check 3: Mandatory POIs
    meta = result.get("meta", {})
    missed_mandatory = meta.get("missed_mandatory", [])
    if missed_mandatory:
        errors.append(f"Missed mandatory POIs: {missed_mandatory}")

    # Check 4: Hotel event pairing
    all_hotels = set(checkins.keys()) | set(checkouts.keys())
    for hotel_id in all_hotels:
        has_checkin = hotel_id in checkins
        has_checkout = hotel_id in checkouts
        if has_checkin and not has_checkout:
            warnings.append(f"Hotel {hotel_id}: check-in on day {checkins[hotel_id]} but no check-out")
        elif has_checkout and not has_checkin:
            warnings.append(f"Hotel {hotel_id}: check-out on day {checkouts[hotel_id]} but no check-in")

    ok = len(errors) == 0

    _log_event("validate_global_rules", {"ok": ok, "errors": errors, "warnings": warnings}, request_id)

    return {"ok": ok, "errors": errors, "warnings": warnings}


def _normalize_city_name(raw: Optional[str]) -> Optional[str]:
    """Normalize a city name for matching (lowercase, strip whitespace, handle common variants)."""
    if not raw:
        return None
    name = str(raw).strip().lower()

    if "," in name:
        name = name.split(",")[0].strip()
    return name


def _filter_mandatory_for_segment(
    mandatory: Optional[Dict[str, Dict]],
    city_name: str,
    segment_days: List[int],
    places: List[Dict[str, Any]],
) -> Optional[Dict[str, Dict]]:
    """
    Filter mandatory POIs for a specific city segment and convert global days to segment-local days.

    This function is used after allocate_days_to_cities() has determined which days belong
    to which city. It filters mandatory POIs that belong to this city AND have a day
    that falls within this segment's days.

    Args:
        mandatory: Full mandatory dict {poi_id: spec}
        city_name: Target city name
        segment_days: List of global 1-based day numbers for this segment (e.g., [3, 4] for days 3-4)
        places: POIs in the city (to lookup area_name)

    Returns:
        Filtered mandatory dict with segment-local day indices (1-based within segment)
    """
    if not mandatory or not segment_days:
        return None

    city_norm = _normalize_city_name(city_name)
    if not city_norm:
        return mandatory

    # Build lookup of poi_id -> area_name from places
    poi_area_lookup: Dict[str, str] = {}
    for poi in places:
        poi_id = poi.get("id")
        area = poi.get("area_name")
        if poi_id and area:
            poi_area_lookup[poi_id] = area

    # Create a mapping from global day to segment-local day (1-based)
    # e.g., segment_days=[3,4] → {3: 1, 4: 2}
    global_to_local: Dict[int, int] = {}
    for local_idx, global_day in enumerate(segment_days):
        global_to_local[global_day] = local_idx + 1  # 1-based

    filtered: Dict[str, Dict] = {}
    for poi_id, spec in mandatory.items():
        spec = spec or {}

        # Check if POI belongs to this city
        poi_dest = spec.get("poi_destination")
        matches_city = False

        if poi_dest:
            dest_norm = _normalize_city_name(poi_dest)
            if dest_norm and (dest_norm in city_norm or city_norm in dest_norm):
                matches_city = True

        if not matches_city:
            poi_area = poi_area_lookup.get(poi_id)
            if poi_area:
                area_norm = _normalize_city_name(poi_area)
                if area_norm and (area_norm in city_norm or city_norm in area_norm):
                    matches_city = True

        # If no destination info, skip (don't include in all segments)
        if not matches_city:
            continue

        # Check if POI's day is in this segment
        global_day = spec.get("day")

        if global_day is not None:
            global_day = int(global_day)
            if global_day not in global_to_local:
                # This POI's day is not in this segment - skip it
                # (It should be in another segment of the same city, if any)
                continue

            # Convert to segment-local day
            spec_copy = spec.copy()
            spec_copy["day"] = global_to_local[global_day]
            spec_copy["_original_global_day"] = global_day
            filtered[poi_id] = spec_copy
        else:
            # No day constraint - include as-is (can be scheduled on any day in segment)
            filtered[poi_id] = spec.copy()

    return filtered if filtered else None


def run_full_pipeline(
    maut_output: Dict[str, Any],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 20,
    solver: str = "acs",  # "ortools" | "acs"
    user_input: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full itinerary optimization pipeline with multi-city support.

    Stages:
    1) Segment POIs by city
    2) For each city: allocate days, select hotel, build problem, solve
       - Mandatory POIs are filtered by city based on poi_destination
    3) Merge city outputs chronologically
    4) Validate global rules
    5) Enrich and return

    Args:
        maut_output: MAUT output with places and meta
        hotel: Optional explicit hotel (legacy single-city support)
        pacing: "relaxed" | "balanced" | "packed"
        mandatory: Optional mandatory POI constraints {poi_id: {day, window, time_type, poi_destination}}
        time_limit_sec: Solver time limit
        solver: "ortools" | "acs"
        user_input: Optional user preferences

    Returns:
        Pipeline result with status, days, meta
    """
    request_id = maut_output.get("meta", {}).get("request_id") or str(uuid.uuid4())

    try:
        places = maut_output.get("places", [])

        # Multi-city path: segment and process per city
        try:
            cities = segment_by_city(maut_output, request_id)
        except ValueError as e:
            return {
                "status": "error",
                "error": str(e),
                "days": [],
                "meta": {"request_id": request_id},
            }

        # Performance guardrail: max cities
        if len(cities) > MAX_CITIES_PER_REQUEST:
            _log_event(
                "pipeline.error",
                {
                    "error": "request_too_large",
                    "city_count": len(cities),
                    "max_cities": MAX_CITIES_PER_REQUEST,
                },
                request_id,
            )
            return {
                "status": "error",
                "error": f"Too many cities ({len(cities)}), max is {MAX_CITIES_PER_REQUEST}",
                "days": [],
                "meta": {"request_id": request_id},
            }

        # Derive user-provided hotels by city from user_input or meta.hotels
        user_hotels_by_city: Optional[Dict[str, Dict[str, Any]]] = None
        if user_input and isinstance(user_input.get("user_hotels_by_city"), dict):
            user_hotels_by_city = user_input.get("user_hotels_by_city")
        else:
            meta_hotels = (maut_output.get("meta", {}) or {}).get("hotels") or []
            if isinstance(meta_hotels, list) and meta_hotels:
                # Infer city for each user hotel by nearest POI that has a city/admin name
                def _area_name(p: Dict[str, Any]) -> Optional[str]:
                    return p.get("area_name")

                place_cities: List[Tuple[str, float, float]] = []
                for p in places:
                    cname = _area_name(p)
                    if not cname:
                        continue
                    coords = p.get("coordinates") or {}
                    plat = coords.get("lat")
                    plon = coords.get("lng")
                    if plat is None or plon is None:
                        continue
                    place_cities.append((cname, float(plat), float(plon)))

                if place_cities:
                    user_hotels_by_city = {}
                    for h in meta_hotels:
                        hlat = h.get("latitude") or (h.get("coordinates") or {}).get("lat")
                        hlon = h.get("longitude") or (h.get("coordinates") or {}).get("lng")
                        if hlat is None or hlon is None:
                            continue
                        # Find nearest city centroid among POIs
                        best_city = None
                        best_d = None
                        for cname, plat, plon in place_cities:
                            d = (plat - float(hlat)) ** 2 + (plon - float(hlon)) ** 2
                            if best_d is None or d < best_d:
                                best_d = d
                                best_city = cname
                        if best_city:
                            user_hotels_by_city[best_city] = {
                                "id": h.get("id") or h.get("poi_id") or f"user_hotel_{best_city}",
                                "name": h.get("name") or h.get("poi_name", "User Hotel"),
                                "lat": float(hlat),
                                "lon": float(hlon),
                                "source": "user",
                            }

        # Process each city
        all_days: List[Dict[str, Any]] = []
        failed_cities: List[str] = []
        total_distance = 0.0
        city_day_offset = 0  # Track global day offset across city segments

        # Parse dates info for day allocation
        dates_info = maut_output.get("meta", {}).get("dates", {}) or {}
        if dates_info.get("type") == "specific":
            start_str = dates_info.get("start_date")
            end_str = dates_info.get("end_date")
            if start_str and end_str:
                trip_start = date.fromisoformat(str(start_str).split("T")[0])
                trip_end = date.fromisoformat(str(end_str).split("T")[0])
                total_trip_days = (trip_end - trip_start).days + 1
            else:
                trip_start = None
                total_trip_days = maut_output.get("meta", {}).get("num_days", 3)
        else:
            trip_start = None
            total_trip_days = maut_output.get("meta", {}).get("num_days", 3)

        # Build POI city lookup for the allocator
        poi_city_lookup: Dict[str, str] = {}
        for city_name, city_data in cities.items():
            for poi in city_data.get("places", []):
                poi_id = poi.get("id", "")
                base_id = poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id
                poi_city_lookup[base_id] = city_name

        # Also add mandatory POI destinations to lookup
        if mandatory:
            for poi_id, spec in mandatory.items():
                if spec and spec.get("poi_destination"):
                    # Normalize the destination name for matching
                    dest = spec["poi_destination"]
                    dest_norm = _normalize_city_name(dest)
                    # Find matching city in our cities dict
                    for city_name in cities.keys():
                        city_norm = _normalize_city_name(city_name)
                        if city_norm and dest_norm and (city_norm in dest_norm or dest_norm in city_norm):
                            poi_city_lookup[poi_id] = city_name
                            break

        # Use the smart city day allocator that respects mandatory POI day constraints
        # This handles:
        # - User explicit day → city assignments
        # - Mandatory POIs with fixed days (day → city forced by POI destination)
        # - Contiguous block allocation to minimize city switches
        # - Proportional allocation for remaining days
        allocation = allocate_days_to_cities(
            cities=cities,
            total_days=total_trip_days,
            mandatory=mandatory,
            user_input=user_input,
            poi_city_lookup=poi_city_lookup,
            trip_start=trip_start,
            request_id=request_id,
        )

        # Log the allocation for debugging
        logger.info(
            f"City day allocation result: day_to_city={allocation.day_to_city}, "
            f"city_order={allocation.city_order}, switches={allocation.city_switches}"
        )

        # Build a global fallback hotel from all accommodations across all cities
        # This is used when a cluster has no accommodations
        global_fallback_hotel = _find_global_fallback_hotel(maut_output, request_id)

        # Build list of all accommodations for nearest-search fallback
        all_accommodations = [poi for poi in places if "accommodation" in poi.get("roles", [])]

        # Process cities in the order they appear in the trip (may repeat if non-contiguous)
        # Group consecutive days by city for batch processing
        city_segments: List[Tuple[str, List[int]]] = []
        current_city = None
        current_days: List[int] = []

        for day_num in range(1, total_trip_days + 1):
            city = allocation.day_to_city.get(day_num)
            if city is None:
                continue
            if city == current_city:
                current_days.append(day_num)
            else:
                if current_city and current_days:
                    city_segments.append((current_city, current_days))
                current_city = city
                current_days = [day_num]

        if current_city and current_days:
            city_segments.append((current_city, current_days))

        # Track hotels for each segment to handle city transitions
        segment_hotels: List[Dict[str, Any]] = []

        # First pass: select hotels for all segments
        for segment_idx, (area_name, segment_days) in enumerate(city_segments):
            maut_city = cities.get(area_name)
            if not maut_city:
                segment_hotels.append(None)
                continue

            allocated_days = len(segment_days)
            if allocated_days == 0:
                segment_hotels.append(None)
                continue

            maut_city["meta"]["num_days"] = allocated_days

            hotel_city = select_hotel_for_city(
                maut_city,
                allocated_days,
                user_hotels_by_city,
                request_id,
                global_fallback_hotel=global_fallback_hotel,
                all_accommodations=all_accommodations,
            )
            segment_hotels.append(hotel_city if hotel_city.get("status") != "error" else None)

        # Process each city segment with proper hotel event handling
        for segment_idx, (area_name, segment_days) in enumerate(city_segments):
            maut_city = cities.get(area_name)
            if not maut_city:
                logger.warning(f"City {area_name} not found in cities dict")
                continue

            allocated_days = len(segment_days)
            if allocated_days == 0:
                continue

            # Calculate start date for this segment
            segment_start_day = segment_days[0]  # 1-based
            if trip_start:
                segment_start_date = trip_start + timedelta(days=segment_start_day - 1)
            else:
                segment_start_date = None

            maut_city["meta"]["num_days"] = allocated_days

            hotel_city = segment_hotels[segment_idx]
            if hotel_city is None or hotel_city.get("status") == "error":
                failed_cities.append(area_name)
                logger.error(f"Hotel selection failed for {area_name}")
                continue

            # Determine city position for hotel event handling
            is_first_city = segment_idx == 0
            is_last_city = segment_idx == len(city_segments) - 1

            # Get previous city's hotel for transition day handling
            prev_city_hotel = None
            if segment_idx > 0 and segment_hotels[segment_idx - 1] is not None:
                prev_city_hotel = segment_hotels[segment_idx - 1]

            try:
                # Filter mandatory POIs for this segment's days
                # The allocation already ensures mandatory POIs are on the correct days for this city
                # We just need to filter by city and convert global days to segment-local days
                segment_mandatory = _filter_mandatory_for_segment(
                    mandatory=mandatory,
                    city_name=area_name,
                    segment_days=segment_days,  # List of global day numbers for this segment
                    places=maut_city.get("places", []),
                )

                # Log mandatory POI filtering for debugging
                if segment_mandatory:
                    logger.info(
                        f"Mandatory POIs for {area_name} segment {segment_idx} "
                        f"(days {segment_days}): {list(segment_mandatory.keys())}"
                    )

                if solver == "acs":
                    # Build problem with proper hotel event handling
                    day_specs, nodes, travel = build_problem(
                        maut_city,
                        hotel_city,
                        pacing=pacing,
                        mandatory=segment_mandatory,
                        is_first_city=is_first_city,
                        is_last_city=is_last_city,
                        prev_city_hotel=prev_city_hotel,
                    )
                    _log_event(
                        "build_problem.call",
                        {
                            "area_name": area_name,
                            "nodes_count": len(nodes),
                            "depot": hotel_city.get("id", "unknown"),
                        },
                        request_id,
                    )

                    cvrptw_output = run_acs_cvrptw(
                        day_specs=day_specs,
                        nodes=nodes,
                        travel=travel,
                        meals_required=3,
                        mandatory=segment_mandatory,
                        cfg=vrp_config,
                    )
                    # _log_event(
                    #     "solver.run",
                    #     {
                    #         "area_name": area_name,
                    #         "solver": "acs",
                    #         "days_count": len(cvrptw_output.get("days", [])),
                    #         "total_candidates": max(0, len(nodes) - 1),
                    #     },
                    #     request_id,
                    # )
                else:
                    cvrptw_output = run_cvrptw(
                        maut_output=maut_city,
                        hotel=hotel_city,
                        pacing=pacing,
                        mandatory=segment_mandatory,
                        time_limit_sec=time_limit_sec,
                    )
                    _log_event(
                        "solver.run",
                        {
                            "area_name": area_name,
                            "solver": "ortools",
                            "days_count": len(cvrptw_output.get("days", [])),
                        },
                        request_id,
                    )

                city_days = cvrptw_output.get("days", [])
                if not city_days:
                    _log_event(
                        "solver.infeasible",
                        {
                            "area_name": area_name,
                            "reason": cvrptw_output.get("note", "No days returned"),
                        },
                        request_id,
                    )
                    failed_cities.append(area_name)
                    continue

                # Add city/destination/depot and absolute dates/weekday for this segment
                for idx, day in enumerate(city_days):
                    day["area_name"] = area_name
                    day["destination"] = area_name
                    day["depot_id"] = hotel_city.get("id")
                    day["source"] = hotel_city.get("source", "maut")

                    # Calculate the global day number for this day in the segment
                    global_day_num = (
                        segment_days[idx] if idx < len(segment_days) else segment_days[-1] + idx - len(segment_days) + 1
                    )

                    if segment_start_date is not None:
                        d = segment_start_date + timedelta(days=idx)
                        day["date"] = d.isoformat()
                        day["weekday"] = d.strftime("%A")
                    else:
                        if day.get("date"):
                            try:
                                d = date.fromisoformat(str(day["date"]).split("T")[0])
                                day["weekday"] = d.strftime("%A")
                            except Exception:
                                day["weekday"] = f"Day {global_day_num}"
                        else:
                            day["weekday"] = f"Day {global_day_num}"

                all_days.extend(city_days)

                # Track total distance from city solver
                city_distance = cvrptw_output.get("meta", {}).get("total_distance", 0)
                total_distance += city_distance

            except Exception:
                logger.exception(f"Solver failed for city {area_name}")
                failed_cities.append(area_name)
            finally:
                # Always increment the day offset for the next city
                city_day_offset += allocated_days

        if not all_days:
            _log_event(
                "pipeline.complete",
                {"status": "error", "total_days": 0, "solver": solver, "failed_cities": failed_cities},
                request_id,
            )
            return {
                "status": "error",
                "error": "No days generated for any city",
                "days": [],
                "meta": {"request_id": request_id, "solver": solver, "failed_cities": failed_cities},
            }

        # Enrich stops and calculate distances
        method_tag = "acs_cvrptw" if solver == "acs" else "cvrptw"

        # Reset total_distance for accurate calculation from enriched stops
        total_distance = 0.0

        for day in all_days:
            original_stops = day.get("stops", [])
            enriched_stops = _enrich_stops_with_coords(original_stops, maut_output)
            day["stops"] = enriched_stops
            day["optimization_method"] = method_tag
            day["total_distance"] = _calculate_day_distance(enriched_stops)
            total_distance += day["total_distance"]

        # Add weekdays
        _add_weekdays_to_days(all_days, maut_output)

        # Track mandatory POIs that were actually visited
        visited_poi_ids: set = set()
        for day in all_days:
            for stop in day.get("stops", []):
                poi_id = stop.get("poi_id", "")
                base_id = poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id
                visited_poi_ids.add(base_id)

        # Find missed mandatory POIs and convert to ideas
        missed_mandatory: List[Dict[str, Any]] = []
        mandatory_ideas: List[Dict[str, Any]] = []

        if mandatory:
            for poi_id, spec in mandatory.items():
                if poi_id not in visited_poi_ids:
                    missed_mandatory.append(
                        {
                            "poi_id": poi_id,
                            "poi_name": spec.get("poi_name", "Unknown"),
                            "reason": "Could not be scheduled within time constraints",
                        }
                    )
                    # Create an idea entry for this unfulfilled mandatory POI
                    mandatory_ideas.append(
                        {
                            "id": poi_id,
                            "name": spec.get("poi_name", "Unknown"),
                            "category": spec.get("role", "attraction"),
                            "categories": spec.get("themes", []),
                            "rating": None,
                            "reviews_count": None,
                            "roles": [spec.get("role", "attraction")],
                            "themes": spec.get("themes", []),
                            "location": spec.get("poi_destination", ""),
                            "images": spec.get("images", [None])[0] if spec.get("images") else None,
                            "reason_not_scheduled": "Mandatory POI could not fit in itinerary due to time/day constraints",
                            "requested_day": spec.get("day"),
                            "time_type": spec.get("time_type"),
                        }
                    )

        # Build result
        total_stops = sum(len(day.get("stops", [])) for day in all_days)

        result = {
            "status": "success" if not failed_cities else "partial_success",
            "days": all_days,
            "meta": {
                "request_id": request_id,
                "total_distance": round(total_distance, 2),
                "total_stops": total_stops,
                "pacing": pacing,
                "solver": solver,
            },
        }

        if failed_cities:
            result["meta"]["failed_cities"] = failed_cities

        if missed_mandatory:
            result["meta"]["missed_mandatory"] = missed_mandatory

        if mandatory_ideas:
            result["meta"]["mandatory_ideas"] = mandatory_ideas

        # Validate global rules
        validation = validate_global_rules(result, None, request_id)
        if not validation["ok"]:
            result["meta"]["validation_errors"] = validation["errors"]

        _log_event(
            "pipeline.complete",
            {
                "status": result["status"],
                "total_days": len(all_days),
                "total_distance": round(total_distance, 2),
                "failed_cities": failed_cities,
            },
            request_id,
        )

        # _save_debug_output(result, request_id)

        return result

    except Exception as e:
        logger.exception("Pipeline execution failed")
        _log_event("pipeline.error", {"error": str(e)}, request_id)
        return {
            "status": "error",
            "error": str(e),
            "days": [],
            "meta": {"request_id": request_id},
        }


# Helpers


def _save_debug_output(result: Dict[str, Any], request_id: str) -> None:
    """Save pipeline output to storage for debugging."""
    from pathlib import Path
    import datetime

    storage_dir = Path(__file__).parent.parent.parent / "storage"
    storage_dir.mkdir(exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"itinerary_debug_{timestamp}_{request_id[:8]}.json"
    filepath = storage_dir / filename

    try:
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"Debug output saved to {filepath}")
    except Exception as e:
        logger.warning(f"Failed to save debug output: {e}")


def _enrich_stops_with_coords(
    stops: List[Dict[str, Any]],
    maut_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Enrich stops with full coordinate information from MAUT output.

    Uses MAUT places list and strips `_dayX` suffix from poi_id when matching.
    """
    poi_lookup: Dict[str, Dict[str, Any]] = {}

    for poi in maut_output.get("places", []):
        poi_id = poi.get("id")
        if not poi_id:
            continue

        entry: Dict[str, Any] = {}
        coords = poi.get("coordinates")
        if coords and coords.get("lat") is not None and coords.get("lng") is not None:
            entry["latitude"] = coords["lat"]
            entry["longitude"] = coords["lng"]

        images = poi.get("images")
        if images:
            entry["images"] = images if isinstance(images, list) else [images]

        if entry:
            poi_lookup[poi_id] = entry

    enriched: List[Dict[str, Any]] = []
    for stop in stops:
        stop_copy = stop.copy()
        poi_id = stop.get("poi_id", "")

        base_poi_id = poi_id.rsplit("_day", 1)[0]

        if base_poi_id in poi_lookup:
            # Only update fields not already present
            for key, val in poi_lookup[base_poi_id].items():
                if key not in stop_copy:
                    stop_copy[key] = val
        elif poi_id in poi_lookup:
            for key, val in poi_lookup[poi_id].items():
                if key not in stop_copy:
                    stop_copy[key] = val
        else:
            # Fallback: check if stop already has coords
            if stop.get("lat") is not None and stop.get("lon") is not None:
                stop_copy["latitude"] = stop["lat"]
                stop_copy["longitude"] = stop["lon"]

        enriched.append(stop_copy)

    return enriched


def _calculate_day_distance(stops: List[Dict[str, Any]]) -> float:
    """
    Calculate total distance for a day's route using OSRM if available,
    otherwise Haversine fallback (via osrm_client wrapper).

    Handles multiple coordinate field formats.
    """
    if len(stops) < 2:
        return 0.0

    from app.services.osrm import osrm_client

    total = 0.0
    for i in range(len(stops) - 1):
        # Try multiple coordinate field formats
        lat1 = stops[i].get("coordinates", {}).get("lat") or stops[i].get("latitude")
        lon1 = stops[i].get("coordinates", {}).get("lng") or stops[i].get("longitude")
        lat2 = stops[i + 1].get("coordinates", {}).get("lat") or stops[i + 1].get("latitude")
        lon2 = stops[i + 1].get("coordinates", {}).get("lng") or stops[i + 1].get("longitude")

        if all(x is not None for x in (lat1, lon1, lat2, lon2)):
            try:
                total += osrm_client.distance(lat1, lon1, lat2, lon2)
            except Exception as e:
                logger.warning(f"Distance calculation failed for segment {i}: {e}")

    return round(total, 2)


def _add_weekdays_to_days(days: List[Dict[str, Any]], maut_output: Dict[str, Any]) -> None:
    """
    Add weekday field to each day based on dates.

    For specific dates: calculates actual weekday from date
    For flexible dates: uses "Day 1", "Day 2", etc.
    """
    meta = maut_output.get("meta", {})
    dates_info = meta.get("dates", {})
    date_type = dates_info.get("type")

    if date_type == "specific":
        start_date_str = dates_info.get("start_date")
        if start_date_str:
            try:
                start_date = date.fromisoformat(str(start_date_str).split("T")[0])
                for idx, day in enumerate(days):
                    current_date = start_date + timedelta(days=idx)
                    day["weekday"] = current_date.strftime("%A")
            except (ValueError, AttributeError):
                for idx, day in enumerate(days):
                    day["weekday"] = f"Day {idx + 1}"
        else:
            for idx, day in enumerate(days):
                day["weekday"] = f"Day {idx + 1}"
    else:
        for idx, day in enumerate(days):
            day["weekday"] = f"Day {idx + 1}"
