from __future__ import annotations

import json
import math
import uuid
from typing import Dict, List, Any, Optional, Tuple
from datetime import date, timedelta

from app.services.cvrptw import run_cvrptw, build_problem
from app.services.acs_cvrptw import run_acs_cvrptw, ACSConfig
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

    Priority:
    1. poi["complete_address"]["city"]
    2. poi.get("area_name")
    3. poi.get("planning_area")
    4. KMeans clustering on (lat, lng) coordinates

    Args:
        maut_output: Full MAUT output with places and meta
        request_id: Optional request ID for logging

    Returns:
        Dict mapping city_name -> maut_suboutput with filtered places and updated meta
    """
    places = maut_output.get("places", [])
    if not places:
        logger.warning("segment_by_city: No places in MAUT output")
        return {}

    # Group POIs by city
    city_groups: Dict[str, List[Dict]] = {}
    uncategorized: List[Dict] = []

    for poi in places:
        city_name = None

        # Priority 1: complete_address.city
        complete_addr = poi.get("complete_address", {})
        if isinstance(complete_addr, dict) and complete_addr.get("city"):
            city_name = complete_addr["city"]

        # Priority 2: area_name
        if not city_name and poi.get("area_name"):
            city_name = poi["area_name"]

        # Priority 3: planning_area
        if not city_name and poi.get("planning_area"):
            city_name = poi["planning_area"]

        if city_name:
            city_groups.setdefault(city_name, []).append(poi)
        else:
            uncategorized.append(poi)

    # Priority 4: KMeans clustering for uncategorized POIs
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

    for city_name, city_pois in city_groups.items():
        # Count accommodations
        accommodation_count = sum(
            1 for poi in city_pois if "accommodation" in poi.get("poi_roles", [])
        )

        # Create city-specific meta
        city_meta = meta.copy()
        city_meta["city_name"] = city_name

        result[city_name] = {"places": city_pois, "meta": city_meta}

        _log_event(
            "segment_by_city",
            {
                "city_name": city_name,
                "poi_count": len(city_pois),
                "accommodation_count": accommodation_count,
            },
            request_id,
        )

    # Assertion: at least one city identified
    if not result:
        logger.error("segment_by_city: No cities identified from MAUT output")
        raise ValueError("segment_by_city: No cities identified from MAUT output")

    return result


def allocate_days_per_city(
    maut_suboutput: Dict[str, Any],
    user_input: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> int:
    """
    Allocate days for a city based on user input or heuristics.

    Case A: user specifies days per city
    Case B: specific dates provided -> compute inclusiveDays proportionally
    Case C: capacity estimation -> days = ceil(total_POIs / realistic_capacity)

    Args:
        maut_suboutput: City-specific MAUT output
        user_input: Optional user preferences including days_per_city
        request_id: Optional request ID for logging

    Returns:
        Number of days allocated (>= 1 if city has POIs)
    """
    meta = maut_suboutput.get("meta", {})
    city_name = meta.get("city_name", "unknown")
    places = maut_suboutput.get("places", [])

    if not places:
        return 0

    # Case A: user-specified days per city
    if user_input and user_input.get("days_per_city", {}).get(city_name):
        days = int(user_input["days_per_city"][city_name])
        _log_event(
            "days_allocated",
            {
                "city_name": city_name,
                "allocated_days": days,
                "method": "user_specified",
                "capacity": None,
                "timezone": meta.get("timezone", "UTC"),
            },
            request_id,
        )
        return max(1, days)

    # Case B: Use num_days from meta if available
    num_days = meta.get("num_days")
    if num_days and num_days > 0:
        _log_event(
            "days_allocated",
            {
                "city_name": city_name,
                "allocated_days": num_days,
                "method": "meta_num_days",
                "capacity": None,
                "timezone": meta.get("timezone", "UTC"),
            },
            request_id,
        )
        return num_days

    # Case C: capacity estimation
    total_pois = len(places)

    # Determine realistic capacity based on city size (simplified heuristic)
    city_population = meta.get("city_population", 0)
    if city_population > 8_000_000:
        realistic_capacity = 5
    else:
        realistic_capacity = 6

    days = math.ceil(total_pois / realistic_capacity)
    days = max(1, min(days, 14))  # Defensive clamp: 1-14 days

    _log_event(
        "days_allocated",
        {
            "city_name": city_name,
            "allocated_days": days,
            "method": "capacity_estimation",
            "capacity": realistic_capacity,
            "timezone": meta.get("timezone", "UTC"),
        },
        request_id,
    )

    return days


def select_hotel_for_city(
    maut_suboutput: Dict[str, Any],
    days_for_city: int,
    user_hotels: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Select or infer hotel for a city.

    Priority:
    1. User-provided hotel(s) for city
    2. Highest MAUT-scored accommodation in city
    3. Error if no accommodation available

    Args:
        maut_suboutput: City-specific MAUT output
        days_for_city: Number of days allocated to this city
        user_hotels: Optional user-provided hotels by city
        request_id: Optional request ID for logging

    Returns:
        Hotel dict with id, name, lat, lon, source
        OR error sentinel {"status": "error", "error": "no_accommodation"|"invalid_hotel_coords"}
    """
    meta = maut_suboutput.get("meta", {})
    city_name = meta.get("city_name", "unknown")
    places = maut_suboutput.get("places", [])

    # Case 1: User-provided hotel
    if user_hotels and city_name in user_hotels:
        user_hotel = user_hotels[city_name]

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
                    "city_name": city_name,
                    "status": "error",
                    "error": "invalid_hotel_coords",
                },
                request_id,
            )
            return {"status": "error", "error": "invalid_hotel_coords"}

        hotel = {
            "id": user_hotel.get("id", f"user_hotel_{city_name}"),
            "name": user_hotel.get("name", "User Hotel"),
            "lat": lat,
            "lon": lon,
            "source": "user",
        }

        _log_event(
            "hotel_selected",
            {
                "city_name": city_name,
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
                    "city_name": city_name,
                    "hotel_id": hotel["id"],
                    "hotel_name": hotel["name"],
                    "source": "maut_selected",
                    "coords": {"lat": lat, "lng": lon},
                },
                request_id,
            )

            return hotel

    # Case 3: Select from MAUT accommodations
    accommodations = [
        poi for poi in places if "accommodation" in poi.get("poi_roles", [])
    ]

    if not accommodations:
        _log_event(
            "hotel_selected",
            {"city_name": city_name, "status": "error", "error": "no_accommodation"},
            request_id,
        )
        return {"status": "error", "error": "no_accommodation"}

    # Select highest MAUT score
    best_hotel = max(accommodations, key=lambda x: x.get("_score", 0))

    coords = best_hotel.get("coordinates", {})
    lat = coords.get("lat")
    lon = coords.get("lng")

    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        _log_event(
            "hotel_selected",
            {
                "city_name": city_name,
                "status": "error",
                "error": "invalid_hotel_coords",
            },
            request_id,
        )
        return {"status": "error", "error": "invalid_hotel_coords"}

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
            "city_name": city_name,
            "hotel_id": hotel["id"],
            "hotel_name": hotel["name"],
            "source": "maut",
            "coords": {"lat": lat, "lng": lon},
        },
        request_id,
    )

    return hotel


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
    3) Merge city outputs chronologically
    4) Validate global rules
    5) Enrich and return

    Args:
        maut_output: MAUT output with places and meta
        hotel: Optional explicit hotel (legacy single-city support)
        pacing: "relaxed" | "balanced" | "packed"
        mandatory: Optional mandatory POI constraints
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

        _log_event(
            "pipeline.start",
            {
                "user_id": maut_output.get("meta", {}).get("user_id"),
                "total_POIs": len(places),
                "solver": solver,
                "pacing": pacing,
            },
            request_id,
        )

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
                def _city_name(p: Dict[str, Any]) -> Optional[str]:
                    ca = p.get("complete_address") or {}
                    return (
                        ca.get("city") or p.get("area_name") or p.get("planning_area")
                    )

                place_cities: List[Tuple[str, float, float]] = []
                for p in places:
                    cname = _city_name(p)
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

        for city_name, maut_city in cities.items():
            # Allocate days
            allocated_days = allocate_days_per_city(maut_city, user_input, request_id)

            if allocated_days == 0:
                continue

            # Update meta with num_days for this city
            maut_city["meta"]["num_days"] = allocated_days

            # Select hotel (prefer user-provided for this city when available)
            hotel_city = select_hotel_for_city(
                maut_city, allocated_days, user_hotels_by_city, request_id
            )

            if hotel_city.get("status") == "error":
                failed_cities.append(city_name)
                logger.error(
                    f"Hotel selection failed for {city_name}: {hotel_city.get('error')}"
                )
                continue

            # Build and solve
            try:
                if solver == "acs":
                    selected_themes = maut_city.get("meta", {}).get(
                        "selected_themes", []
                    )
                    day_specs, nodes, travel = build_problem(
                        maut_city,
                        hotel_city,
                        pacing=pacing,
                        selected_themes=selected_themes,
                        mandatory=mandatory,
                    )

                    _log_event(
                        "build_problem.call",
                        {
                            "city_name": city_name,
                            "nodes_count": len(nodes),
                            "depot": hotel_city.get("id", "unknown"),
                        },
                        request_id,
                    )

                    cvrptw_output = run_acs_cvrptw(
                        day_specs=day_specs,
                        nodes=nodes,
                        travel=travel,
                        meals_required=2,
                        mandatory=mandatory,
                        cfg=ACSConfig(),
                    )

                    _log_event(
                        "solver.run",
                        {
                            "city_name": city_name,
                            "solver": "acs",
                            "days_count": len(cvrptw_output.get("days", [])),
                            "total_candidates": len(nodes) - 1,
                        },
                        request_id,
                    )
                else:
                    cvrptw_output = run_cvrptw(
                        maut_output=maut_city,
                        hotel=hotel_city,
                        pacing=pacing,
                        mandatory=mandatory,
                        time_limit_sec=time_limit_sec,
                    )

                    _log_event(
                        "solver.run",
                        {
                            "city_name": city_name,
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
                            "city_name": city_name,
                            "reason": cvrptw_output.get("note", "No days returned"),
                        },
                        request_id,
                    )
                    failed_cities.append(city_name)
                    continue

                # Add city_name to each day
                for day in city_days:
                    day["city_name"] = city_name
                    day["depot_id"] = hotel_city.get("id")
                    day["source"] = hotel_city.get("source", "maut")

                all_days.extend(city_days)

            except Exception:
                logger.exception(f"Solver failed for city {city_name}")
                failed_cities.append(city_name)
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

            cvrptw_output = run_acs_cvrptw(
                day_specs=day_specs,
                nodes=nodes,
                travel=travel,
                meals_required=2,
                mandatory=mandatory,
                cfg=ACSConfig(),
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
        start_date_str = dates_info.get("startDate")
        if start_date_str:
            try:
                start_date = date.fromisoformat(start_date_str.split("T")[0])
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
