from __future__ import annotations

from typing import Dict, List, Any, Optional
import numpy as np

from app.services.cvrptw import run_cvrptw, build_problem
from app.services.ant_colony_opt import AntColonyOptimizer, ACOConfig
from app.services.acs_cvrptw import run_acs_cvrptw, ACSConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


# Basic distance (used only inside ACO for its internal matrix)
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


# ACO refinement (TSP on a single day)
def optimize_day_route_with_aco(
    stops: List[Dict[str, Any]],
    config: Optional[ACOConfig] = None,
) -> List[Dict[str, Any]]:
    """
    Optimize the order of stops within a single day using Ant Colony Optimization.

    Assumptions:
    - All stops are already feasible w.r.t. CVRPTW constraints.
    - This only reorders POIs to reduce travel distance.
    - Distance inside ACO uses haversine; final reporting uses OSRM.
    """
    if len(stops) <= 2:
        return stops

    # Separate depot (hotel) from POIs
    depot_stops = [s for s in stops if s.get("role") in ("hotel", "depot")]
    poi_stops = [s for s in stops if s.get("role") not in ("hotel", "depot")]

    if len(poi_stops) <= 1:
        return stops

    # Extract coordinates for POI stops
    coordinates: List[List[float]] = []
    for stop in poi_stops:
        lat = stop.get("latitude") or stop.get("coordinates", {}).get("lat")
        lon = stop.get("longitude") or stop.get("coordinates", {}).get("lng")
        if lat is None or lon is None:
            logger.warning(
                "Stop %s missing coordinates, skipping ACO", stop.get("name")
            )
            return stops
        coordinates.append([lat, lon])

    coords_array = np.array(coordinates, dtype=np.float64)

    # Build distance matrix (geometric; OSRM used later for actual km)
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
    aco_cfg = config or ACOConfig(
        n_ants=20,
        n_iterations=50,
        alpha=1.0,
        beta=2.0,
        evaporation_rate=0.5,
        n_best=5,
    )
    aco = AntColonyOptimizer(dist_matrix, aco_cfg)
    best_path, best_distance = aco.optimize()
    logger.info(
        "ACO optimized route: %d POI stops, distance=%.2fkm (haversine)",
        len(poi_stops),
        best_distance,
    )

    # Reorder stops according to ACO solution
    optimized_pois = [poi_stops[i] for i in best_path]

    # Reconstruct full day: depot → optimized POIs → depot
    result: List[Dict[str, Any]] = []
    if depot_stops:
        result.append(depot_stops[0])  # start at hotel

    result.extend(optimized_pois)

    if len(depot_stops) > 1:
        result.append(depot_stops[-1])  # explicit end hotel
    elif depot_stops:
        # If only one depot in original, add same as return
        result.append(depot_stops[0])

    return result


# Pipeline


def run_full_pipeline(
    maut_output: Dict[str, Any],
    hotel: Optional[Dict[str, Any]] = None,
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 15,
    use_aco: bool = True,
    aco_config: Optional[ACOConfig] = None,
    solver: str = "ortools",  # "ortools" | "acs"
) -> Dict[str, Any]:
    """
    Full itinerary optimization pipeline.

    Stages:
    1) Build CVRPTW problem from MAUT output (build_problem in cvrptw).
    2a) If solver == "ortools": solve CVRPTW with OR-Tools (run_cvrptw).
    2b) If solver == "acs":    solve CVRPTW with ACS-CVRPTW (run_acs_cvrptw).
    3) Optional ACO refinement on each day (TSP reordering), only for OR-Tools.

    - OR-Tools and ACS-CVRPTW both use OSRM-derived travel matrices
      for scheduling (with haversine fallback inside osrm_client).
    - ACO uses haversine internally but final distances are computed
      with OSRM for all variants so comparison is on the same metric.
    """
    try:
        # Hotel selection
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
                logger.info("Using hotel from MAUT: %s", hotel["name"])
            else:
                return {
                    "status": "error",
                    "error": "No hotel provided and no hotel selected by MAUT",
                    "days": [],
                }

        # ACO refinement is only defined on the OR-Tools CVRPTW solver path
        if solver == "acs" and use_aco:
            logger.info("Solver is 'acs'; disabling ACO refinement (not supported).")
            use_aco = False

        # Step 1: Solve CVRPTW
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
                meals_required=2,  # tune as needed
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
            }

        days = cvrptw_output.get("days", [])
        if not days:
            return {
                "status": "error",
                "error": cvrptw_output.get("note", "CVRPTW returned no days"),
                "days": [],
            }

        # Step 2: Enrich + optional ACO refinement
        if solver == "ortools" and use_aco:
            logger.info(
                "Applying ACO algorithm to optimize intra-day route sequences..."
            )
            for day in days:
                original_stops = day.get("stops", [])

                # Enrich OR-Tools stops with coordinates
                enriched_cvrptw_stops = _enrich_stops_with_coords(
                    original_stops, maut_output
                )
                day["stops_cvrptw"] = enriched_cvrptw_stops

                if len(enriched_cvrptw_stops) > 2:
                    optimized_stops = optimize_day_route_with_aco(
                        enriched_cvrptw_stops, aco_config
                    )
                    day["stops_aco"] = optimized_stops
                    day["stops"] = optimized_stops  # final schedule for frontend
                    day["optimization_method"] = "cvrptw+aco"
                else:
                    day["stops_aco"] = enriched_cvrptw_stops
                    day["stops"] = enriched_cvrptw_stops
                    day["optimization_method"] = "cvrptw"

                day["total_distance_cvrptw"] = _calculate_day_distance(
                    day["stops_cvrptw"]
                )
                day["total_distance_aco"] = _calculate_day_distance(day["stops_aco"])
                day["total_distance"] = day["total_distance_aco"]
        else:
            # No ACO refinement; still enrich for consistent OSRM distance
            method_tag = "acs_cvrptw" if solver == "acs" else "cvrptw"
            for day in days:
                original_stops = day.get("stops", [])
                enriched_cvrptw_stops = _enrich_stops_with_coords(
                    original_stops, maut_output
                )
                day["stops_cvrptw"] = enriched_cvrptw_stops
                day["stops"] = enriched_cvrptw_stops
                day["optimization_method"] = method_tag
                day["total_distance_cvrptw"] = _calculate_day_distance(
                    day["stops_cvrptw"]
                )
                day["total_distance"] = day["total_distance_cvrptw"]

        # Step 3: Global metrics
        total_distance = sum(day.get("total_distance", 0.0) for day in days)
        total_stops = sum(len(day.get("stops", [])) for day in days)

        result = {
            "status": "success",
            "days": days,
            "meta": {
                "total_distance": round(total_distance, 2),
                "total_stops": total_stops,
                "optimization_applied": use_aco,
                "pacing": pacing,
                "solver": solver,
            },
        }

        logger.info(
            "Pipeline complete: %d days, %d stops, %.2fkm total",
            len(result["days"]),
            total_stops,
            total_distance,
        )
        return result

    except Exception as e:
        logger.exception("Pipeline execution failed")
        return {
            "status": "error",
            "error": str(e),
            "days": [],
        }


# Helpers


def _enrich_stops_with_coords(
    stops: List[Dict[str, Any]],
    maut_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Enrich stops with full coordinate information from MAUT output.

    Uses MAUT places list and strips `_dayX` suffix from poi_id when matching.
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
        elif poi.get("latitude") is not None and poi.get("longitude") is not None:
            poi_lookup[poi_id] = {
                "latitude": poi["latitude"],
                "longitude": poi["longitude"],
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

        enriched.append(stop_copy)

    return enriched


def _calculate_day_distance(stops: List[Dict[str, Any]]) -> float:
    """
    Calculate total distance for a day's route using OSRM if available,
    otherwise Haversine fallback (via osrm_client wrapper).
    """
    if len(stops) < 2:
        return 0.0

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

        if all(x is not None for x in (lat1, lon1, lat2, lon2)):
            total += osrm_client.distance(lat1, lon1, lat2, lon2)

    return round(total, 2)
