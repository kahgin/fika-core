from __future__ import annotations

import numpy as np
from typing import Dict, List, Any, Optional
from app.services.cvrptw import run_cvrptw
from app.services.ant_colony_opt import AntColonyOptimizer, ACOConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate haversine distance in km between two coordinates."""
    from math import radians, sin, cos, sqrt, atan2

    R = 6371.0  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def optimize_day_route_with_aco(
    stops: List[Dict[str, Any]], config: Optional[ACOConfig] = None
) -> List[Dict[str, Any]]:
    """
    Optimize the order of stops within a single day using Ant Colony Optimization.

    Args:
        stops: List of stops with coordinates (lat, lon)
        config: ACO configuration (optional)

    Returns:
        Reordered list of stops with optimized route
    """
    if len(stops) <= 2:
        # No optimization needed for 0-2 stops
        return stops

    # Separate depot (hotel) from other stops
    depot_stops = [s for s in stops if s.get("role") in ["hotel", "depot"]]
    poi_stops = [s for s in stops if s.get("role") not in ["hotel", "depot"]]

    if len(poi_stops) <= 1:
        # No optimization needed
        return stops

    # Extract coordinates for POI stops
    coordinates = []
    for stop in poi_stops:
        lat = stop.get("latitude") or stop.get("coordinates", {}).get("lat")
        lon = stop.get("longitude") or stop.get("coordinates", {}).get("lng")
        if lat is None or lon is None:
            logger.warning(f"Stop {stop.get('name')} missing coordinates, skipping ACO")
            return stops
        coordinates.append([lat, lon])

    coords_array = np.array(coordinates, dtype=np.float64)

    # Build distance matrix
    n = len(coords_array)
    dist_matrix = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            dist = haversine_distance(
                coords_array[i][0],
                coords_array[i][1],
                coords_array[j][0],
                coords_array[j][1],
            )
            dist_matrix[i, j] = dist
            dist_matrix[j, i] = dist

    # Run ACO
    aco_config = config or ACOConfig(
        n_ants=20,
        n_iterations=50,
        alpha=1.0,
        beta=2.0,
        evaporation_rate=0.5,
        n_best=5,
    )

    aco = AntColonyOptimizer(dist_matrix, aco_config)
    best_path, best_distance = aco.optimize()

    logger.info(
        f"ACO optimized route: {len(poi_stops)} stops, distance={best_distance:.2f}km"
    )

    # Reorder stops according to ACO solution
    optimized_pois = [poi_stops[i] for i in best_path]

    # Reconstruct full day: start depot → optimized POIs → end depot
    result = []
    if depot_stops and len(depot_stops) > 0:
        result.append(depot_stops[0])  # Start at hotel
    result.extend(optimized_pois)
    if depot_stops and len(depot_stops) > 1:
        result.append(depot_stops[-1])  # End at hotel
    elif depot_stops and len(depot_stops) == 1:
        # Add hotel return if only one depot in original
        result.append(depot_stops[0])

    return result


def run_full_pipeline(
    maut_output: Dict[str, Any],
    hotel: Optional[Dict[str, Any]] = None,
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 15,
    use_aco: bool = True,
    aco_config: Optional[ACOConfig] = None,
) -> Dict[str, Any]:
    """
    Run full optimization pipeline: MAUT → CVRPTW Problem → Route Optimization.

    Pipeline stages:
    1. CVRPTW problem construction: Convert MAUT output into formal problem model
    2. OR-Tools solver: Solve CVRPTW to assign POIs to days (respects all constraints)
    3. ACO refinement (optional): Optimize visit order within each day (TSP subproblem)

    Args:
        maut_output: Output from MAUT pipeline
        hotel: Hotel information {"id": str, "name": str, "lat": float, "lon": float}
               If None, uses selected_hotel from MAUT meta
        pacing: Pacing preference ("relaxed" | "balanced" | "packed")
        mandatory: Mandatory POI constraints
        time_limit_sec: OR-Tools solver time limit for CVRPTW
        use_aco: Whether to apply ACO algorithm to optimize each day's route
        aco_config: ACO algorithm configuration (optional)

    Returns:
        {
            "status": "success" | "error",
            "days": [
                {
                    "date": str,
                    "stops": [...],
                    "meals": int,
                    "total_distance": float,
                    "optimization_method": "cvrptw" | "cvrptw+aco"
                }
            ],
            "meta": {
                "total_distance": float,
                "total_stops": int,
                "optimization_applied": bool
            }
        }
    """
    try:
        # Use hotel from MAUT meta if not provided
        if hotel is None:
            selected_hotel = maut_output.get("meta", {}).get("selected_hotel")
            if selected_hotel:
                coords = selected_hotel.get("coordinates") or {}
                hotel = {
                    "id": selected_hotel["id"],
                    "name": selected_hotel["name"],
                    "lat": coords.get("lat") or selected_hotel.get("latitude"),
                    "lon": coords.get("lng") or selected_hotel.get("longitude"),
                }
                logger.info(f"Using hotel from MAUT: {hotel['name']}")
            else:
                return {
                    "status": "error",
                    "error": "No hotel provided and no hotel selected by MAUT",
                    "days": [],
                }
        # Step 1: Solve CVRPTW problem model using OR-Tools
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
            }

        # Check if CVRPTW returned empty days (failure case)
        days = cvrptw_output.get("days", [])
        if not days:
            return {
                "status": "error",
                "error": cvrptw_output.get("note", "CVRPTW returned no days"),
                "days": [],
            }

        # Step 2: Apply ACO algorithm to refine daily routes
        if use_aco:
            logger.info("Applying ACO algorithm to optimize intra-day route sequences...")
            for day in cvrptw_output["days"]:
                original_stops = day.get("stops", [])

                # Enrich CVRPTW solution with coordinates
                enriched_cvrptw_stops = _enrich_stops_with_coords(original_stops, maut_output)
                day["stops_cvrptw"] = enriched_cvrptw_stops

                if len(enriched_cvrptw_stops) > 2:
                    optimized_stops = optimize_day_route_with_aco(
                        enriched_cvrptw_stops, aco_config
                    )
                    day["stops_aco"] = optimized_stops
                    day["optimization_method"] = "cvrptw+aco"
                else:
                    # Too few POIs to bother optimizing; keep CVRPTW order
                    day["stops_aco"] = enriched_cvrptw_stops
                    day["optimization_method"] = "cvrptw"

                # Distances: both paths use the same distance function and full coords
                day["total_distance_cvrptw"] = _calculate_day_distance(day["stops_cvrptw"])
                day["total_distance_aco"] = _calculate_day_distance(day["stops_aco"])
                day["total_distance"] = day["total_distance_aco"]  # primary metric
        else:
            # CVRPTW only: still enrich stops so distance isn't 0
            for day in cvrptw_output["days"]:
                original_stops = day.get("stops", [])
                enriched_cvrptw_stops = _enrich_stops_with_coords(original_stops, maut_output)
                day["stops_cvrptw"] = enriched_cvrptw_stops
                day["optimization_method"] = "cvrptw"
                day["total_distance_cvrptw"] = _calculate_day_distance(day["stops_cvrptw"])
                day["total_distance"] = day["total_distance_cvrptw"]


        # Step 3: Calculate overall metrics
        total_distance = sum(
            day.get("total_distance", 0) for day in cvrptw_output["days"]
        )
        total_stops = sum(len(day.get("stops", [])) for day in cvrptw_output["days"])

        result = {
            "status": "success",
            "days": cvrptw_output["days"],
            "meta": {
                "total_distance": round(total_distance, 2),
                "total_stops": total_stops,
                "optimization_applied": use_aco,
                "pacing": pacing,
            },
        }

        logger.info(
            f"Pipeline complete: {len(result['days'])} days, "
            f"{total_stops} stops, {total_distance:.2f}km total"
        )

        return result

    except Exception as e:
        logger.exception("Pipeline execution failed")
        return {
            "status": "error",
            "error": str(e),
            "days": [],
        }


def _enrich_stops_with_coords(
    stops: List[Dict[str, Any]], maut_output: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Enrich stops with full coordinate information from MAUT output.

    Args:
        stops: List of stops from CVRPTW (may have limited info)
        maut_output: Full MAUT output with POI details

    Returns:
        Enriched stops with coordinates
    """
    # Build POI lookup from MAUT output
    poi_lookup = {}
    for poi in maut_output.get("places", []):
        poi_id = poi.get("id")
        if poi_id:
            # Store coordinates
            coords = poi.get("coordinates")
            if coords:
                poi_lookup[poi_id] = {
                    "latitude": coords.get("lat"),
                    "longitude": coords.get("lng"),
                }
            elif poi.get("latitude") and poi.get("longitude"):
                poi_lookup[poi_id] = {
                    "latitude": poi.get("latitude"),
                    "longitude": poi.get("longitude"),
                }

    # Enrich stops
    enriched = []
    for stop in stops:
        stop_copy = stop.copy()
        poi_id = stop.get("poi_id", "")

        # Strip _dayX suffix if present
        base_poi_id = poi_id.rsplit("_day", 1)[0]

        # Try to find coordinates
        if base_poi_id in poi_lookup:
            stop_copy.update(poi_lookup[base_poi_id])
        elif poi_id in poi_lookup:
            stop_copy.update(poi_lookup[poi_id])

        enriched.append(stop_copy)

    return enriched


def _calculate_day_distance(stops: List[Dict[str, Any]]) -> float:
    """
    Calculate total distance for a day's route.

    Args:
        stops: List of stops with coordinates

    Returns:
        Total distance in km
    """
    if len(stops) < 2:
        return 0.0

    # Import here to avoid circular dependency
    from app.services.osrm import osrm_client

    total = 0.0
    for i in range(len(stops) - 1):
        lat1 = stops[i].get("latitude") or stops[i].get("coordinates", {}).get("lat")
        lon1 = stops[i].get("longitude") or stops[i].get("coordinates", {}).get("lng")
        lat2 = stops[i + 1].get("latitude") or stops[i + 1].get("coordinates", {}).get(
            "lat"
        )
        lon2 = stops[i + 1].get("longitude") or stops[i + 1].get("coordinates", {}).get(
            "lng"
        )

        if all(x is not None for x in [lat1, lon1, lat2, lon2]):
            # Use OSRM if available and requested, otherwise Haversine
            distance = osrm_client.distance(lat1, lon1, lat2, lon2)
            total += distance

    return round(total, 2)
