"""
Recompute API: Re-optimize itineraries with different strategies.

Modes:
1. full: Strip everything, regenerate from meta + mandatory POIs
2. partial: Keep current POIs, re-optimize schedule
3. single_day: Re-optimize a specific day only
"""

from fastapi import APIRouter, HTTPException
from app.api.itinerary import (
    load_itinerary,
    save_itinerary,
    _normalize_destination_name,
)
from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.vrp_utils import build_problem
from app.services.vrp_model import vrp_config

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["recompute"])


@router.post("/itinerary/{itin_id}/recompute")
def recompute_itinerary(itin_id: str, payload: dict):
    """
    Recompute itinerary with different strategies.

    Payload:
    {
        "mode": "full" | "partial" | "single_day",
        "day_index": int (required for single_day mode),
        "options": {
            "pacing": str (optional, override pacing),
            "meals_required": int (optional, default 3)
        }
    }

    Modes:
    - full: Regenerate from scratch using meta + mandatory POIs
    - partial: Re-optimize current POIs in schedule
    - single_day: Re-optimize specific day only

    Returns:
        Updated itinerary data
    """
    try:
        mode = payload.get("mode", "partial")
        day_index = payload.get("day_index")
        options = payload.get("options", {})

        if mode not in ("full", "partial", "single_day"):
            raise HTTPException(
                status_code=400,
                detail="Invalid mode. Must be 'full', 'partial', or 'single_day'",
            )

        if mode == "single_day" and day_index is None:
            raise HTTPException(
                status_code=400, detail="day_index required for single_day mode"
            )

        data = load_itinerary(itin_id)

        if mode == "full":
            result = _recompute_full(data, options)
        elif mode == "partial":
            result = _recompute_partial(data, options)
        else:  # single_day
            result = _recompute_single_day(data, day_index, options)

        # Recompute day labels based on dates info
        from app.utils.date_utils import recompute_day_labels

        if result.get("plan", {}).get("days"):
            recompute_day_labels(result["plan"]["days"], result.get("meta", {}).get("dates"))

        save_itinerary(itin_id, result)
        logger.info(f"Recomputed itinerary {itin_id} with mode={mode}")

        # Transform to frontend format
        from app.services.transformers import transform_itinerary_response_to_frontend

        return transform_itinerary_response_to_frontend(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to recompute itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


def _recompute_full(data: dict, options: dict) -> dict:
    """
    Full recompute: Strip days & items, regenerate from meta + mandatory POIs.

    This is equivalent to creating a new itinerary with the same parameters.
    """
    meta = data.get("meta", {})

    # Build payload from meta
    payload = {
        "title": meta.get("title"),
        "destinations": meta.get("destinations", []),
        "dates": meta.get("dates", {}),
        "travelers": meta.get("travelers", {}),
        "preferences": meta.get("preferences", {}),
        "dietary_restrictions": meta.get("dietary_restrictions"),
        "hotels": meta.get("hotels", []),
        "mandatory_pois": meta.get("mandatory_pois", []),
        "flags": meta.get("flags", {}),
    }

    # Override pacing if provided
    if options.get("pacing"):
        payload.setdefault("preferences", {})["pacing"] = options["pacing"]

    # Transform and run MAUT
    maut_request = transform_frontend_payload(payload)

    destinations = payload.get("destinations", [])
    if destinations:
        # Multi-city flow
        all_places = []
        selected_themes_union = []

        for d in destinations:
            raw_city = d.get("city")
            city = _normalize_destination_name(raw_city)
            if not city:
                continue

            req_i = dict(maut_request)
            req_i["destination"] = city
            out_i = run_maut(req_i)

            if out_i.get("places"):
                for p in out_i.get("places", []):
                    p["requested_city"] = city
                all_places.extend(out_i.get("places", []))

                th = out_i.get("meta", {}).get("selected_themes", [])
                for t in th:
                    if t not in selected_themes_union:
                        selected_themes_union.append(t)

        maut_output = {
            "status": "ok",
            "places": all_places,
            "meta": {
                "selected_themes": selected_themes_union[:3]
                if selected_themes_union
                else [],
            },
        }
    else:
        # Single-city flow
        maut_output = run_maut(maut_request)

    # Enrich with dates
    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = payload.get("dates", {})
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # Add hotels and mandatory POIs to places
    places = maut_output.get("places", [])

    for hotel_data in payload.get("hotels", []):
        hotel_destination = hotel_data.get("destination")
        hotel_area_name = (
            _normalize_destination_name(hotel_destination)
            if hotel_destination
            else None
        )

        hotel_poi = {
            "id": hotel_data.get("poi_id"),
            "name": hotel_data.get("poi_name", "Hotel"),
            "coordinates": {
                "lat": hotel_data.get("latitude"),
                "lng": hotel_data.get("longitude"),
            },
            "poi_roles": [hotel_data.get("role", "accommodation")],
            "themes": hotel_data.get("themes", []),
            "images": hotel_data.get("images", []),
            "source": "user",
        }
        if hotel_area_name:
            hotel_poi["area_name"] = hotel_area_name

        if not any(p.get("id") == hotel_poi["id"] for p in places):
            places.append(hotel_poi)

    # Build mandatory dict
    mandatory = {}
    for poi in payload.get("mandatory_pois", []):
        poi_id = poi.get("poi_id")
        if not poi_id:
            continue

        mandatory[poi_id] = {
            "time_type": poi.get("time_type", "any_time"),
            "day": poi.get("day"),
            "poi_destination": poi.get("poi_destination"),
        }

        if poi.get("time_type") == "all_day":
            mandatory[poi_id]["all_day"] = True
        elif poi.get("start_time") and poi.get("end_time"):
            mandatory[poi_id]["window"] = [poi.get("start_time"), poi.get("end_time")]

        # Add to places
        mandatory_poi = {
            "id": poi_id,
            "name": poi.get("poi_name", "POI"),
            "coordinates": {
                "lat": poi.get("latitude"),
                "lng": poi.get("longitude"),
            },
            "poi_roles": [poi.get("role", "attraction")],
            "area_name": poi.get("poi_destination"),
            "themes": poi.get("themes", []),
            "images": poi.get("images", []),
        }
        if not any(p.get("id") == mandatory_poi["id"] for p in places):
            places.append(mandatory_poi)

    maut_output["places"] = places

    # Run pipeline
    pipeline_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=None,  # Let pipeline select
        pacing=maut_request.get("pacing", "balanced"),
        mandatory=mandatory if mandatory else None,
        time_limit_sec=20,
        solver="acs",
    )

    # Update data
    data["plan"] = {
        "status": "ok",
        "days": pipeline_output.get("days", []),
        "items": [_transform_poi_to_frontend(p) for p in places],
        "meta": pipeline_output.get("meta", {}),
    }

    return data


def _recompute_partial(data: dict, options: dict) -> dict:
    """
    Partial recompute: Keep current POIs, re-optimize schedule with ACS.

    Uses existing POIs from plan.items and re-runs ACS-CVRPTW.
    """
    plan = data.get("plan", {})
    days = plan.get("days", [])
    items = plan.get("items", [])
    meta = data.get("meta", {})

    if not days or not items:
        raise HTTPException(status_code=400, detail="No existing plan to recompute")

    # Build MAUT output from existing items
    places = []
    for item in items:
        places.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "coordinates": item.get("coordinates", {}),
                "poi_roles": item.get("roles", []),
                "themes": item.get("themes", []),
                "images": item.get("images", []),
                "open_hours": item.get("openHours"),
            }
        )

    maut_output = {
        "status": "ok",
        "places": places,
        "meta": {
            "dates": meta.get("dates", {}),
            "num_days": meta.get("num_days", len(days)),
        },
    }

    # Extract hotel from first day
    first_day = days[0] if days else {}
    depot_id = first_day.get("depot_id")
    hotel = None

    if depot_id:
        for stop in first_day.get("stops", []):
            if stop.get("poi_id") == depot_id:
                hotel = {
                    "id": depot_id,
                    "name": stop.get("name", "Hotel"),
                    "lat": stop.get("latitude"),
                    "lon": stop.get("longitude"),
                }
                break

    # Build mandatory from meta
    mandatory = {}
    for poi in meta.get("mandatory_pois", []):
        poi_id = poi.get("poi_id")
        if poi_id:
            mandatory[poi_id] = {
                "time_type": poi.get("time_type", "any_time"),
                "day": poi.get("day"),
            }

    # Run pipeline
    pacing = options.get("pacing") or meta.get("preferences", {}).get(
        "pacing", "balanced"
    )

    pipeline_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=pacing,
        mandatory=mandatory if mandatory else None,
        time_limit_sec=15,
        solver="acs",
    )

    # Update days
    data["plan"]["days"] = pipeline_output.get("days", [])
    data["plan"]["meta"] = pipeline_output.get("meta", {})

    return data


def _recompute_single_day(data: dict, day_index: int, options: dict) -> dict:
    """
    Single-day recompute: Re-optimize a specific day only with ACS.

    Keeps other days unchanged, re-runs ACS for the specified day.
    """
    plan = data.get("plan", {})
    days = plan.get("days", [])
    items = plan.get("items", [])
    meta = data.get("meta", {})

    if day_index < 0 or day_index >= len(days):
        raise HTTPException(status_code=400, detail="Invalid day_index")

    target_day = days[day_index]

    # Build places from items
    places = []
    for item in items:
        places.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "coordinates": item.get("coordinates", {}),
                "poi_roles": item.get("roles", []),
                "themes": item.get("themes", []),
                "images": item.get("images", []),
                "open_hours": item.get("openHours"),
            }
        )

    # Get hotel for this day
    depot_id = target_day.get("depot_id")
    hotel = None

    # Try to find hotel from stops
    if depot_id:
        for stop in target_day.get("stops", []):
            if stop.get("poi_id") == depot_id:
                hotel = {
                    "id": depot_id,
                    "name": stop.get("name", "Hotel"),
                    "lat": stop.get("latitude"),
                    "lon": stop.get("longitude"),
                }
                break

    # Fallback: try to get hotel from meta.hotels
    if not hotel:
        hotels_from_meta = meta.get("hotels", [])
        if hotels_from_meta:
            first_hotel = hotels_from_meta[0]
            hotel = {
                "id": first_hotel.get("poi_id"),
                "name": first_hotel.get("poi_name", "Hotel"),
                "lat": first_hotel.get("latitude"),
                "lon": first_hotel.get("longitude"),
            }

    # Fallback: try to find any accommodation in stops
    if not hotel:
        for stop in target_day.get("stops", []):
            if "accommodation" in (stop.get("role") or ""):
                hotel = {
                    "id": stop.get("poi_id"),
                    "name": stop.get("name", "Hotel"),
                    "lat": stop.get("latitude"),
                    "lon": stop.get("longitude"),
                }
                break

    # Final fallback: use first stop as reference point
    if not hotel and target_day.get("stops"):
        first_stop = target_day["stops"][0]
        hotel = {
            "id": first_stop.get("poi_id"),
            "name": first_stop.get("name", "Start Point"),
            "lat": first_stop.get("latitude"),
            "lon": first_stop.get("longitude"),
        }

    if not hotel:
        raise HTTPException(status_code=400, detail="No hotel or reference point found for day")

    # Build single-day problem
    from datetime import date as _date, timedelta

    dates_info = meta.get("dates", {})
    if dates_info.get("type") == "specific" and dates_info.get("start_date"):
        start_date = _date.fromisoformat(str(dates_info["start_date"]).split("T")[0])
        day_date = start_date + timedelta(days=day_index)
    else:
        day_date = _date.today()

    maut_output = {
        "status": "ok",
        "places": places,
        "meta": {
            "dates": dates_info,
            "num_days": 1,
        },
    }

    pacing = options.get("pacing") or meta.get("preferences", {}).get(
        "pacing", "balanced"
    )

    # Build problem for single day
    day_specs, nodes, travel = build_problem(
        maut_output,
        hotel,
        pacing=pacing,
        mandatory=None,
    )

    if not day_specs:
        raise HTTPException(status_code=400, detail="Failed to build problem for day")

    # Run ACS for single day
    meals_required = options.get("meals_required", 3)

    result = run_acs_cvrptw(
        day_specs=[day_specs[0]],
        nodes=nodes,
        travel=travel,
        meals_required=meals_required,
        cfg=vrp_config,
    )

    if not result.get("days"):
        raise HTTPException(status_code=500, detail="Failed to optimize day")

    # Update the specific day
    new_day = result["days"][0]
    new_day["date"] = target_day.get("date")
    new_day["weekday"] = target_day.get("weekday")
    new_day["area_name"] = target_day.get("area_name")
    new_day["destination"] = target_day.get("destination")
    new_day["depot_id"] = depot_id
    new_day["source"] = target_day.get("source")
    new_day["optimization_method"] = "acs_cvrptw"

    days[day_index] = new_day
    data["plan"]["days"] = days

    return data


def _transform_poi_to_frontend(poi: dict) -> dict:
    """Transform POI to frontend format."""
    coords = poi.get("coordinates") or {}
    category = None
    if poi.get("categories") and len(poi["categories"]) > 0:
        category = poi["categories"][0]
    elif poi.get("category"):
        category = poi["category"]

    return {
        "id": poi.get("id"),
        "name": poi.get("name"),
        "category": category,
        "categories": poi.get("categories", [category] if category else []),
        "rating": poi.get("review_rating") or poi.get("rating"),
        "reviewCount": poi.get("review_count"),
        "location": None,
        "images": poi.get("images", []),
        "roles": poi.get("poi_roles", []),
        "poiRoles": poi.get("poi_roles", []),
        "themes": poi.get("themes", []),
        "description": poi.get("description"),
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("google_maps_url"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "openHours": poi.get("open_hours"),
        "priceLevel": poi.get("price_level"),
    }
