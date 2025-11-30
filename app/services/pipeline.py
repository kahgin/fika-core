from __future__ import annotations

from typing import Dict, List, Any, Optional
from datetime import date, timedelta

from app.services.cvrptw import run_cvrptw, build_problem
from app.services.acs_cvrptw import run_acs_cvrptw, ACSConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


def run_full_pipeline(
    maut_output: Dict[str, Any],
    hotel: Optional[Dict[str, Any]] = None,
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 20,
    solver: str = "ortools",  # "ortools" | "acs"
) -> Dict[str, Any]:
    """
    Full itinerary optimization pipeline.

    Stages:
    1) Build CVRPTW problem from MAUT output (build_problem in cvrptw).
    2a) If solver == "ortools": solve CVRPTW with OR-Tools (run_cvrptw).
    2b) If solver == "acs":    solve CVRPTW with ACS-CVRPTW (run_acs_cvrptw).

    - Both solver uses the same OSRM-derived travel matrices
      for scheduling (with haversine fallback inside osrm_client).
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

        # Step 2: Enrich stops with coordinates and compute OSRM distance
        method_tag = "acs_cvrptw" if solver == "acs" else "cvrptw"

        for day in days:
            original_stops = day.get("stops", [])
            enriched_stops = _enrich_stops_with_coords(original_stops, maut_output)

            # This is the only stops array the frontend sees
            day["stops"] = enriched_stops
            day["optimization_method"] = method_tag
            day["total_distance"] = _calculate_day_distance(enriched_stops)

        # Step 3: Add weekdays to days
        _add_weekdays_to_days(days, maut_output)

        # Step 4: Global metrics
        total_distance = sum(day.get("total_distance", 0.0) for day in days)
        total_stops = sum(len(day.get("stops", [])) for day in days)

        result = {
            "status": "success",
            "days": days,
            "meta": {
                "total_distance": round(total_distance, 2),
                "total_stops": total_stops,
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
        # Parse start date and calculate weekdays
        start_date_str = dates_info.get("startDate")
        if start_date_str:
            try:
                start_date = date.fromisoformat(start_date_str.split("T")[0])
                for idx, day in enumerate(days):
                    current_date = start_date + timedelta(days=idx)
                    day["weekday"] = current_date.strftime(
                        "%A"
                    )  # Monday, Tuesday, etc.
            except (ValueError, AttributeError):
                # Fallback to Day N if date parsing fails
                for idx, day in enumerate(days):
                    day["weekday"] = f"Day {idx + 1}"
        else:
            # No start date, use Day N
            for idx, day in enumerate(days):
                day["weekday"] = f"Day {idx + 1}"
    else:
        # Flexible dates: use Day N
        for idx, day in enumerate(days):
            day["weekday"] = f"Day {idx + 1}"
