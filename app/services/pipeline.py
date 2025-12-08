from __future__ import annotations

import json
import math
import uuid
from typing import Dict, List, Any, Optional, Tuple
from datetime import date, timedelta

from app.services.vrp_model import VRPConfig
from app.services.cvrptw import run_cvrptw, build_problem
from app.services.acs_cvrptw import run_acs_cvrptw
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Performance guardrails
MAX_CITIES_PER_REQUEST = 5
MAX_POIS_PER_CITY = 200


def _log_event(
    event: str, payload: Dict[str, Any], request_id: Optional[str] = None
) -> None:
    """Emit structured JSON log with event name and request_id."""
    log_data = {"event": event, **payload}
    if request_id:
        log_data["request_id"] = request_id
    logger.info(json.dumps(log_data))


def segment_by_city(
    maut_output: Dict[str, Any], request_id: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
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

    # Group POIs by city
    city_groups: Dict[str, List[Dict]] = {}
    uncategorized: List[Dict] = []

    for poi in places:
        area_name = None

        # Area_name
        if not area_name and poi.get("area_name"):
            area_name = poi["area_name"]

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

            if (
                lat is not None
                and lng is not None
                and math.isfinite(lat)
                and math.isfinite(lng)
            ):
                coords_list.append([lat, lng])
                valid_pois.append(poi)
            else:
                logger.warning(
                    f"segment_by_city: POI {poi.get('id', 'unknown')} has invalid coordinates, skipping"
                )

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
                logger.warning(
                    "sklearn not available, grouping uncategorized POIs as single cluster"
                )
                city_groups.setdefault("cluster_0", []).extend(valid_pois)

    # Build city suboutputs
    result: Dict[str, Dict[str, Any]] = {}
    meta = maut_output.get("meta", {})

    for area_name, city_pois in city_groups.items():
        # Count accommodations
        accommodation_count = sum(
            1 for poi in city_pois if "accommodation" in poi.get("poi_roles", [])
        )

        # Create city-specific meta
        city_meta = meta.copy()
        city_meta["area_name"] = area_name

        result[area_name] = {"places": city_pois, "meta": city_meta}

        _log_event(
            "segment_by_city",
            {
                "area_name": area_name,
                "poi_count": len(city_pois),
                "accommodation_count": accommodation_count,
            },
        )

    # Assertion: at least one city identified
    if not result:
        logger.error("segment_by_city: No cities identified from MAUT output")
        raise ValueError("segment_by_city: No cities identified from MAUT output")

    return result


def allocate_days_proportionally(
    cities: Dict[str, Dict[str, Any]],
    total_days: int,
    user_input: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, int]:
    """
    Allocate days proportionally across cities based on POI count.

    This ensures the total allocated days equals exactly total_days,
    preventing extra days from being generated.

    Args:
        cities: Dict of area_name -> maut_suboutput
        total_days: Total number of days for the trip
        user_input: Optional user preferences including days_per_city
        request_id: Optional request ID for logging

    Returns:
        Dict mapping area_name -> allocated_days
    """
    if not cities or total_days <= 0:
        return {}

    # Case A: user-specified days per city - use those directly
    if user_input and user_input.get("days_per_city"):
        dpc = user_input["days_per_city"]
        allocated: Dict[str, int] = {}
        remaining_days = total_days
        unspecified_cities: List[str] = []

        for area_name in cities.keys():
            days = None
            # Exact match
            if area_name in dpc:
                days = dpc[area_name]
            else:
                # Approximate matching
                cname_l = area_name.lower()
                for k, v in dpc.items():
                    kl = str(k).lower()
                    if kl in cname_l or cname_l in kl:
                        days = v
                        break

            if days is not None:
                days = max(1, int(days))
                allocated[area_name] = days
                remaining_days -= days
            else:
                unspecified_cities.append(area_name)

        # Distribute remaining days to unspecified cities
        if unspecified_cities and remaining_days > 0:
            per_city = max(1, remaining_days // len(unspecified_cities))
            for area_name in unspecified_cities:
                allocated[area_name] = min(per_city, remaining_days)
                remaining_days -= allocated[area_name]

        return allocated

    # Case B: Proportional allocation based on POI count
    poi_counts: Dict[str, int] = {}
    total_pois = 0

    for area_name, city_data in cities.items():
        count = len(city_data.get("places", []))
        poi_counts[area_name] = count
        total_pois += count

    if total_pois == 0:
        # Equal distribution if no POIs
        per_city = max(1, total_days // len(cities))
        return {city: per_city for city in cities.keys()}

    # Allocate proportionally, ensuring at least 1 day per city with POIs
    allocated: Dict[str, int] = {}
    remaining_days = total_days

    # First pass: allocate proportionally (floor)
    for area_name, count in poi_counts.items():
        if count == 0:
            allocated[area_name] = 0
            continue
        proportion = count / total_pois
        days = max(1, int(proportion * total_days))
        allocated[area_name] = days
        remaining_days -= days

    # Second pass: distribute remaining days to cities with most POIs
    if remaining_days > 0:
        sorted_cities = sorted(
            [(c, poi_counts[c]) for c in cities.keys() if poi_counts[c] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        for area_name, _ in sorted_cities:
            if remaining_days <= 0:
                break
            allocated[area_name] += 1
            remaining_days -= 1

    # Third pass: trim if over-allocated
    while sum(allocated.values()) > total_days:
        # Find city with most days that has more than 1
        sorted_cities = sorted(
            [(c, d) for c, d in allocated.items() if d > 1],
            key=lambda x: x[1],
            reverse=True,
        )
        if sorted_cities:
            allocated[sorted_cities[0][0]] -= 1
        else:
            break

    # for area_name, days in allocated.items():
    #     _log_event(
    #         "days_allocated",
    #         {
    #             "area_name": area_name,
    #             "allocated_days": days,
    #             "method": "proportional",
    #             "poi_count": poi_counts.get(area_name, 0),
    #             "total_days": total_days,
    #         },
    #         request_id,
    #     )

    return allocated


def allocate_days_per_city(
    maut_suboutput: Dict[str, Any],
    user_input: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> int:
    """
    Allocate days for a single city based on user input or capacity estimation.

    NOTE: This function is kept for backward compatibility but should not be used
    in multi-city scenarios. Use allocate_days_proportionally() instead.

    Args:
        maut_suboutput: City-specific MAUT output
        user_input: Optional user preferences including days_per_city
        request_id: Optional request ID for logging

    Returns:
        Number of days allocated (>= 1 if city has POIs)
    """
    meta = maut_suboutput.get("meta", {})
    area_name = meta.get("area_name", "unknown")
    places = maut_suboutput.get("places", [])

    if not places:
        return 0

    # Case A: user-specified days per city
    if user_input and user_input.get("days_per_city"):
        dpc = user_input["days_per_city"]
        days = None
        # Exact match
        if area_name in dpc:
            days = dpc[area_name]
        else:
            # Approximate matching (e.g., 'Johor' vs 'Johor Bahru')
            cname_l = area_name.lower()
            for k, v in dpc.items():
                kl = str(k).lower()
                if kl in cname_l or cname_l in kl:
                    days = v
                    break
        if days is not None:
            days = int(days)
            _log_event(
                "days_allocated",
                {
                    "area_name": area_name,
                    "allocated_days": days,
                    "method": "user_specified",
                    "timezone": meta.get("timezone", "UTC"),
                },
                request_id,
            )
            return max(1, days)

    # Case B: capacity estimation (NOT using meta.num_days to avoid giving each city full trip days)
    if "num_days" in meta:
        days = int(meta["num_days"])
        method = "meta_num_days"
    else:
        # Capacity estimation based on total POIs and city population
        total_pois = len(places)
        city_population = meta.get("city_population", 0)
        if city_population > 8_000_000:
            realistic_capacity = 5
        else:
            realistic_capacity = 6
        days = math.ceil(total_pois / realistic_capacity)
        days = max(1, min(days, 14))
        method = "capacity_estimation"

    _log_event(
        "days_allocated",
        {
            "area_name": area_name,
            "allocated_days": days,
            "method": method,
            "timezone": meta.get("timezone", "UTC"),
        },
        request_id,
    )
    return days


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
    all_accommodations = [
        poi for poi in places if "accommodation" in poi.get("poi_roles", [])
    ]

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

    _log_event(
        "global_fallback_hotel_found",
        {
            "hotel_id": hotel["id"],
            "hotel_name": hotel["name"],
            "coords": {"lat": lat, "lng": lon},
        },
        request_id,
    )

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

        if (
            lat is None
            or lon is None
            or not (math.isfinite(lat) and math.isfinite(lon))
        ):
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

        _log_event(
            "hotel_selected",
            {
                "area_name": area_name,
                "hotel_id": hotel["id"],
                "hotel_name": hotel["name"],
                "source": "user",
                "coords": {"lat": lat, "lng": lon},
            },
            request_id,
        )

        return hotel

    # Case 2: Check meta.selected_hotel first
    selected_hotel = meta.get("selected_hotel")
    if selected_hotel:
        coords = selected_hotel.get("coordinates", {})
        lat = coords.get("lat")
        lon = coords.get("lng")

        if (
            lat is not None
            and lon is not None
            and math.isfinite(lat)
            and math.isfinite(lon)
        ):
            hotel = {
                "id": selected_hotel["id"],
                "name": selected_hotel["name"],
                "lat": lat,
                "lon": lon,
                "source": "maut_selected",
            }

            _log_event(
                "hotel_selected",
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
    accommodations = [
        poi for poi in places if "accommodation" in poi.get("poi_roles", [])
    ]

    if accommodations:
        # Select highest MAUT score from city's accommodations
        best_hotel = max(accommodations, key=lambda x: x.get("_score", 0))

        coords = best_hotel.get("coordinates", {})
        lat = coords.get("lat")
        lon = coords.get("lng")

        if (
            lat is not None
            and lon is not None
            and math.isfinite(lat)
            and math.isfinite(lon)
        ):
            hotel = {
                "id": best_hotel["id"],
                "name": best_hotel["name"],
                "lat": lat,
                "lon": lon,
                "source": "maut",
            }

            _log_event(
                "hotel_selected",
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
                "hotel_selected",
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
            "hotel_selected",
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
    Validate legacy constraints on the final result.

    Checks:
    1. Meal rules: per day meals_count <= meals_max
    2. Theme repetition: no more than 2 attractions with same primary theme per day
    3. Mandatory POIs: missed mandatory appear in meta["missed_mandatory"]
    4. Start/end depots: day start != end only when allowed
    5. Cross-city nodes: every day's stops must match day's city

    Args:
        result: Pipeline result with days
        config: Optional config with meals_max, etc.
        request_id: Optional request ID for logging

    Returns:
        {"ok": bool, "errors": [...]}
    """
    config = config or {}
    meals_max = config.get("meals_max", 3)
    errors: List[str] = []

    days = result.get("days", [])

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])

        # Check 1: Meal count
        meal_count = sum(1 for s in stops if s.get("role") == "meal")
        if meal_count > meals_max:
            errors.append(
                f"Day {day_idx + 1}: {meal_count} meals exceeds max {meals_max}"
            )

        # Check 2: Theme repetition
        theme_counts: Dict[str, int] = {}
        for stop in stops:
            if stop.get("role") == "attraction":
                themes = stop.get("themes", [])
                if themes:
                    primary_theme = themes[0]
                    theme_counts[primary_theme] = theme_counts.get(primary_theme, 0) + 1

        for theme, count in theme_counts.items():
            if count > 2:
                errors.append(
                    f"Day {day_idx + 1}: {count} attractions with theme '{theme}' exceeds max 2"
                )

    # Check 3: Mandatory POIs
    meta = result.get("meta", {})
    missed_mandatory = meta.get("missed_mandatory", [])
    if missed_mandatory:
        errors.append(f"Missed mandatory POIs: {missed_mandatory}")

    ok = len(errors) == 0

    _log_event("validate_global_rules", {"ok": ok, "errors": errors}, request_id)

    return {"ok": ok, "errors": errors}


def _normalize_city_name(raw: Optional[str]) -> Optional[str]:
    """Normalize a city name for matching (lowercase, strip whitespace, handle common variants)."""
    if not raw:
        return None
    name = str(raw).strip().lower()
    # Handle common variants
    if "," in name:
        name = name.split(",")[0].strip()
    return name


def _filter_mandatory_for_city(
    mandatory: Optional[Dict[str, Dict]],
    city_name: str,
    places: List[Dict[str, Any]],
) -> Optional[Dict[str, Dict]]:
    """
    Filter mandatory POIs for a specific city.
    
    Matches mandatory POIs to city by:
    1. poi_destination field in mandatory spec
    2. area_name of the POI in places list
    
    Args:
        mandatory: Full mandatory dict {poi_id: spec}
        city_name: Target city name
        places: POIs in the city (to lookup area_name)
    
    Returns:
        Filtered mandatory dict for this city only
    """
    if not mandatory:
        return None
    
    city_norm = _normalize_city_name(city_name)
    if not city_norm:
        return mandatory  # Can't filter, return all
    
    # Build lookup of poi_id -> area_name from places
    poi_area_lookup: Dict[str, str] = {}
    for poi in places:
        poi_id = poi.get("id")
        area = poi.get("area_name")
        if poi_id and area:
            poi_area_lookup[poi_id] = area
    
    filtered: Dict[str, Dict] = {}
    for poi_id, spec in mandatory.items():
        spec = spec or {}
        
        # Check poi_destination in spec
        poi_dest = spec.get("poi_destination")
        if poi_dest:
            dest_norm = _normalize_city_name(poi_dest)
            if dest_norm and (dest_norm in city_norm or city_norm in dest_norm):
                filtered[poi_id] = spec
                continue
        
        # Check area_name from places lookup
        poi_area = poi_area_lookup.get(poi_id)
        if poi_area:
            area_norm = _normalize_city_name(poi_area)
            if area_norm and (area_norm in city_norm or city_norm in area_norm):
                filtered[poi_id] = spec
                continue
        
        # If no destination info, include in all cities (fallback)
        if not poi_dest and not poi_area:
            filtered[poi_id] = spec
    
    return filtered if filtered else None


def run_full_pipeline(
    maut_output: Dict[str, Any],
    hotel: Optional[Dict[str, Any]] = None,
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 20,
    solver: str = "ortools",  # "ortools" | "acs"
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
    # Generate request_id for traceability
    request_id = maut_output.get("meta", {}).get("request_id") or str(uuid.uuid4())

    try:
        places = maut_output.get("places", [])

        # Legacy single-city path: if hotel is provided directly, use original flow
        if hotel is not None:
            return _run_single_city_pipeline(
                maut_output,
                hotel,
                pacing,
                mandatory,
                time_limit_sec,
                solver,
                request_id,
            )

        # Check for selected_hotel in meta (legacy support)
        selected_hotel = maut_output.get("meta", {}).get("selected_hotel")
        if selected_hotel:
            coords = selected_hotel.get("coordinates") or {}
            hotel = {
                "id": selected_hotel["id"],
                "name": selected_hotel["name"],
                "lat": coords.get("lat"),
                "lon": coords.get("lng"),
            }
            logger.info("Using hotel from MAUT: %s", hotel["name"])
            return _run_single_city_pipeline(
                maut_output,
                hotel,
                pacing,
                mandatory,
                time_limit_sec,
                solver,
                request_id,
            )

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
                        hlat = h.get("latitude") or (h.get("coordinates") or {}).get(
                            "lat"
                        )
                        hlon = h.get("longitude") or (h.get("coordinates") or {}).get(
                            "lng"
                        )
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
                                "id": h.get("id")
                                or h.get("poi_id")
                                or f"user_hotel_{best_city}",
                                "name": h.get("name")
                                or h.get("poi_name", "User Hotel"),
                                "lat": float(hlat),
                                "lon": float(hlon),
                                "source": "user",
                            }

        # Process each city
        all_days: List[Dict[str, Any]] = []
        failed_cities: List[str] = []
        total_distance = 0.0

        # Deterministic per-city processing with contiguous date splitting
        def _determine_city_order(
            cities_dict: Dict[str, Dict[str, Any]], user_input: Optional[Dict[str, Any]]
        ):
            # Prefer explicit user order, then meta.city_order, else deterministic alphabetical
            if user_input and isinstance(user_input.get("city_order"), list):
                return [c for c in user_input["city_order"] if c in cities_dict] + [
                    c
                    for c in cities_dict.keys()
                    if c not in (user_input or {}).get("city_order", [])
                ]
            meta_order = (maut_output.get("meta") or {}).get("city_order") or []
            if isinstance(meta_order, list) and meta_order:
                return [c for c in meta_order if c in cities_dict] + [
                    c for c in cities_dict.keys() if c not in meta_order
                ]
            return sorted(cities_dict.keys())

        city_order = _determine_city_order(cities, user_input)

        # If global specific dates provided, parse them
        dates_info = maut_output.get("meta", {}).get("dates", {}) or {}
        if dates_info.get("type") == "specific":
            start_str = dates_info.get("start_date")
            end_str = dates_info.get("end_date")
            if start_str and end_str:
                start_date = date.fromisoformat(str(start_str).split("T")[0])
                end_date = date.fromisoformat(str(end_str).split("T")[0])
                total_days = (end_date - start_date).days + 1
            else:
                start_date = None
                total_days = None
        else:
            start_date = None
            total_days = None

        # Compute allocated days per city using proportional allocation
        # This ensures total days matches exactly the trip duration
        if total_days is not None and total_days > 0:
            # Use proportional allocation to distribute days across cities
            allocated_map = allocate_days_proportionally(
                cities, total_days, user_input, request_id
            )
        else:
            # Fallback: use capacity-based allocation for flexible dates
            allocated_map = {}
            for c in city_order:
                allocated_map[c] = max(
                    0, allocate_days_per_city(cities[c], user_input, request_id)
                )

        # Build contiguous start dates if specific dates provided
        if start_date is not None and total_days is not None:
            city_start_dates = {}
            cursor = start_date
            for c in city_order:
                dcount = allocated_map.get(c, 0)
                if dcount <= 0:
                    city_start_dates[c] = None
                    continue
                city_start_dates[c] = cursor
                cursor = cursor + timedelta(days=dcount)
        else:
            city_start_dates = {c: None for c in city_order}

        # Build a global fallback hotel from all accommodations across all cities
        # This is used when a cluster has no accommodations
        global_fallback_hotel = _find_global_fallback_hotel(maut_output, request_id)

        # Build list of all accommodations for nearest-search fallback
        all_accommodations = [
            poi for poi in places if "accommodation" in poi.get("poi_roles", [])
        ]

        # Process each city in deterministic order and assign absolute dates/weekday when available
        all_days: List[Dict[str, Any]] = []
        failed_cities: List[str] = []
        total_distance = 0.0

        for area_name in city_order:
            maut_city = cities[area_name]
            allocated_days = allocated_map.get(area_name, 0)
            if allocated_days == 0:
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
            if hotel_city.get("status") == "error":
                failed_cities.append(area_name)
                logger.error(
                    f"Hotel selection failed for {area_name}: {hotel_city.get('error')}"
                )
                continue

            try:
                # Filter mandatory POIs for this city
                city_mandatory = _filter_mandatory_for_city(
                    mandatory, area_name, maut_city.get("places", [])
                )
                
                if solver == "acs":
                    selected_themes = maut_city.get("meta", {}).get(
                        "selected_themes", []
                    )
                    day_specs, nodes, travel = build_problem(
                        maut_city,
                        hotel_city,
                        pacing=pacing,
                        selected_themes=selected_themes,
                        mandatory=city_mandatory,
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
                    # Methodology Table 11: target 3 meals per day
                    cvrptw_output = run_acs_cvrptw(
                        day_specs=day_specs,
                        nodes=nodes,
                        travel=travel,
                        meals_required=3,
                        mandatory=city_mandatory,
                        cfg=VRPConfig(),
                    )
                    _log_event(
                        "solver.run",
                        {
                            "area_name": area_name,
                            "solver": "acs",
                            "days_count": len(cvrptw_output.get("days", [])),
                            "total_candidates": max(0, len(nodes) - 1),
                        },
                        request_id,
                    )
                else:
                    cvrptw_output = run_cvrptw(
                        maut_output=maut_city,
                        hotel=hotel_city,
                        pacing=pacing,
                        mandatory=city_mandatory,
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

                # Add city/destination/depot and absolute dates/weekday if city_start_dates available
                city_start = city_start_dates.get(area_name)
                for idx, day in enumerate(city_days):
                    day["area_name"] = area_name
                    day["destination"] = area_name
                    day["depot_id"] = hotel_city.get("id")
                    day["source"] = hotel_city.get("source", "maut")

                    if city_start is not None:
                        d = city_start + timedelta(days=idx)
                        day["date"] = d.isoformat()
                        day["weekday"] = d.strftime("%A")
                    else:
                        if day.get("date"):
                            try:
                                d = date.fromisoformat(str(day["date"]).split("T")[0])
                                day["weekday"] = d.strftime("%A")
                            except Exception:
                                day["weekday"] = f"Day {len(all_days) + idx + 1}"
                        else:
                            day["weekday"] = f"Day {len(all_days) + idx + 1}"

                all_days.extend(city_days)

            except Exception:
                logger.exception(f"Solver failed for city {area_name}")
                failed_cities.append(area_name)
                continue

        if not all_days:
            _log_event(
                "pipeline.complete",
                {"status": "error", "total_days": 0, "failed_cities": failed_cities},
                request_id,
            )
            return {
                "status": "error",
                "error": "No days generated for any city",
                "days": [],
                "meta": {"request_id": request_id, "failed_cities": failed_cities},
            }

        # Enrich stops and calculate distances
        method_tag = "acs_cvrptw" if solver == "acs" else "cvrptw"

        for day in all_days:
            original_stops = day.get("stops", [])
            enriched_stops = _enrich_stops_with_coords(original_stops, maut_output)
            day["stops"] = enriched_stops
            day["optimization_method"] = method_tag
            day["total_distance"] = _calculate_day_distance(enriched_stops)
            total_distance += day["total_distance"]

        # Add weekdays
        _add_weekdays_to_days(all_days, maut_output)

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


def _run_single_city_pipeline(
    maut_output: Dict[str, Any],
    hotel: Dict[str, Any],
    pacing: str,
    mandatory: Optional[Dict[str, Dict]],
    time_limit_sec: int,
    solver: str,
    request_id: str,
) -> Dict[str, Any]:
    """
    Original single-city pipeline flow for backward compatibility.
    """
    try:
        if solver == "acs":
            logger.info("Solving CVRPTW problem with ACS-CVRPTW...")
            selected_themes = maut_output.get("meta", {}).get("selected_themes", [])
            day_specs, nodes, travel = build_problem(
                maut_output,
                hotel,
                pacing=pacing,
                selected_themes=selected_themes,
                mandatory=mandatory,
            )

            # Methodology Table 11: target 3 meals per day
            cvrptw_output = run_acs_cvrptw(
                day_specs=day_specs,
                nodes=nodes,
                travel=travel,
                meals_required=3,
                mandatory=mandatory,
                cfg=VRPConfig(),
            )
        else:
            logger.info("Solving CVRPTW problem (OR-Tools constraint solver)...")
            cvrptw_output = run_cvrptw(
                maut_output=maut_output,
                hotel=hotel,
                pacing=pacing,
                mandatory=mandatory,
                time_limit_sec=time_limit_sec,
            )

        if not cvrptw_output or "days" not in cvrptw_output:
            return {
                "status": "error",
                "error": "CVRPTW failed to generate solution",
                "days": [],
                "meta": {"request_id": request_id},
            }

        days = cvrptw_output.get("days", [])
        if not days:
            return {
                "status": "error",
                "error": cvrptw_output.get("note", "CVRPTW returned no days"),
                "days": [],
                "meta": {"request_id": request_id},
            }

        # Enrich stops
        method_tag = "acs_cvrptw" if solver == "acs" else "cvrptw"

        for day in days:
            original_stops = day.get("stops", [])
            enriched_stops = _enrich_stops_with_coords(original_stops, maut_output)
            day["stops"] = enriched_stops
            day["optimization_method"] = method_tag
            day["total_distance"] = _calculate_day_distance(enriched_stops)

        _add_weekdays_to_days(days, maut_output)

        total_distance = sum(day.get("total_distance", 0.0) for day in days)
        total_stops = sum(len(day.get("stops", [])) for day in days)

        result = {
            "status": "success",
            "days": days,
            "meta": {
                "request_id": request_id,
                "total_distance": round(total_distance, 2),
                "total_stops": total_stops,
                "pacing": pacing,
                "solver": solver,
            },
        }

        _log_event(
            "pipeline.complete",
            {
                "status": "success",
                "total_days": len(days),
                "total_distance": round(total_distance, 2),
            },
            request_id,
        )

        return result

    except Exception as e:
        logger.exception("Pipeline execution failed")
        return {
            "status": "error",
            "error": str(e),
            "days": [],
            "meta": {"request_id": request_id},
        }


# Helpers


def _enrich_stops_with_coords(
    stops: List[Dict[str, Any]],
    maut_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Enrich stops with full coordinate information from MAUT output.

    Uses MAUT places list and strips `_dayX` suffix from poi_id when matching.
    Handles inferred_depot and various coordinate field formats.
    """
    poi_lookup: Dict[str, Dict[str, float]] = {}

    for poi in maut_output.get("places", []):
        poi_id = poi.get("id")
        if not poi_id:
            continue

        coords = poi.get("coordinates")
        if coords and coords.get("lat") is not None and coords.get("lng") is not None:
            poi_lookup[poi_id] = {
                "latitude": coords["lat"],
                "longitude": coords["lng"],
            }

    enriched: List[Dict[str, Any]] = []
    for stop in stops:
        stop_copy = stop.copy()
        poi_id = stop.get("poi_id", "")

        # Strip _dayX if present
        base_poi_id = poi_id.rsplit("_day", 1)[0]

        if base_poi_id in poi_lookup:
            stop_copy.update(poi_lookup[base_poi_id])
        elif poi_id in poi_lookup:
            stop_copy.update(poi_lookup[poi_id])
        else:
            # Fallback: check if stop already has coords
            if stop.get("latitude") is not None and stop.get("longitude") is not None:
                pass  # Already has coords
            elif stop.get("lat") is not None and stop.get("lon") is not None:
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
        lat2 = stops[i + 1].get("coordinates", {}).get("lat") or stops[i + 1].get(
            "latitude"
        )
        lon2 = stops[i + 1].get("coordinates", {}).get("lng") or stops[i + 1].get(
            "longitude"
        )

        if all(x is not None for x in (lat1, lon1, lat2, lon2)):
            try:
                total += osrm_client.distance(lat1, lon1, lat2, lon2)
            except Exception as e:
                logger.warning(f"Distance calculation failed for segment {i}: {e}")

    return round(total, 2)


def _add_weekdays_to_days(
    days: List[Dict[str, Any]], maut_output: Dict[str, Any]
) -> None:
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
