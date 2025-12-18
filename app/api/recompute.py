"""
Recompute API: Re-optimize itineraries with different strategies.

Modes:
1. full: Strip everything, regenerate from meta + mandatory POIs
2. partial: Keep current POIs from days, re-optimize schedule
3. single_day: Re-optimize a specific day using that day's POIs
"""

from fastapi import APIRouter, HTTPException
from app.db.itinerary_storage import load_itinerary_from_db, save_itinerary_to_db
from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.vrp_utils import build_problem
from app.services.vrp_model import vrp_config

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["recompute"])


def _normalize_destination_name(raw) -> str | None:
    """Normalize location label to city name."""
    if not raw:
        return None
    name = str(raw).strip()
    return name.split(",")[0].strip() if "," in name else name


def _sanitize_images(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if not isinstance(v, list):
        return []
    return [img.strip() for img in v if isinstance(img, str) and img.strip()]


def _extract_pois_from_days(days: list) -> list[dict]:
    """
    Extract unique POIs from itinerary days.

    Returns a list of POI dicts suitable for MAUT/solver input.
    Deduplicates by poi_id.
    """
    seen_ids = set()
    pois = []

    for day in days:
        for stop in day.get("stops", []):
            poi_id = stop.get("poi_id")
            if not poi_id or poi_id in seen_ids:
                continue

            # Skip depot/hotel nodes that aren't accommodation events
            role = stop.get("role", "attraction")
            if role == "depot":
                continue

            seen_ids.add(poi_id)

            # Build POI dict
            coords = stop.get("coordinates") or {}
            lat = coords.get("lat") or stop.get("latitude")
            lng = coords.get("lng") or stop.get("longitude")

            poi = {
                "id": poi_id,
                "name": stop.get("name", "Unknown"),
                "coordinates": {"lat": lat, "lng": lng},
                "roles": [role],
                "themes": stop.get("themes", []),
                "open_hours": stop.get("open_hours"),
                "images": _sanitize_images(stop.get("images")),
            }

            # Preserve area_name/destination for multi-city
            area = stop.get("area_name") or stop.get("destination")
            if area:
                poi["area_name"] = area

            pois.append(poi)

    return pois


def _extract_pois_from_single_day(day: dict) -> list[dict]:
    """Extract POIs from a single day."""
    seen_ids = set()
    pois = []

    for stop in day.get("stops", []):
        poi_id = stop.get("poi_id")
        if not poi_id or poi_id in seen_ids:
            continue

        role = stop.get("role", "attraction")
        if role == "depot":
            continue

        seen_ids.add(poi_id)

        coords = stop.get("coordinates") or {}
        lat = coords.get("lat") or stop.get("latitude")
        lng = coords.get("lng") or stop.get("longitude")

        poi = {
            "id": poi_id,
            "name": stop.get("name", "Unknown"),
            "coordinates": {"lat": lat, "lng": lng},
            "roles": [role],
            "themes": stop.get("themes", []),
            "open_hours": stop.get("open_hours"),
            "images": _sanitize_images(stop.get("images")),
        }

        pois.append(poi)

    return pois


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
    - partial: Re-optimize using POIs currently in days (not items bucket)
    - single_day: Re-optimize specific day using that day's POIs
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
            raise HTTPException(status_code=400, detail="day_index required for single_day mode")

        data = load_itinerary_from_db(itin_id)
        if not data:
            raise HTTPException(status_code=404, detail="Itinerary not found")

        if mode == "full":
            result = _recompute_full(data, options)
        elif mode == "partial":
            result = _recompute_partial(data, options)
        else:
            result = _recompute_single_day(data, day_index, options)

        from app.utils.date_utils import recompute_day_labels

        if result.get("plan", {}).get("days"):
            recompute_day_labels(result["plan"]["days"], result.get("meta", {}).get("dates"))

        if not save_itinerary_to_db(itin_id, result):
            raise HTTPException(status_code=500, detail="Failed to save itinerary")
        logger.info(f"Recomputed itinerary {itin_id} with mode={mode}")

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
    """
    meta = data.get("meta", {})

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

    if options.get("pacing"):
        payload.setdefault("preferences", {})["pacing"] = options["pacing"]

    maut_request = transform_frontend_payload(payload)

    destinations = payload.get("destinations", [])
    if destinations:
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
                "selected_themes": selected_themes_union[:3] if selected_themes_union else [],
            },
        }
    else:
        maut_output = run_maut(maut_request)

    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = payload.get("dates", {})
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    places = maut_output.get("places", [])

    for hotel_data in payload.get("hotels", []):
        hotel_destination = hotel_data.get("destination")
        hotel_area_name = _normalize_destination_name(hotel_destination) if hotel_destination else None

        hotel_poi = {
            "id": hotel_data.get("poi_id"),
            "name": hotel_data.get("poi_name", "Hotel"),
            "coordinates": {
                "lat": hotel_data.get("latitude"),
                "lng": hotel_data.get("longitude"),
            },
            "roles": [hotel_data.get("role", "accommodation")],
            "themes": hotel_data.get("themes", []),
            "images": hotel_data.get("images", []),
            "source": "user",
        }
        if hotel_area_name:
            hotel_poi["area_name"] = hotel_area_name

        if not any(p.get("id") == hotel_poi["id"] for p in places):
            places.append(hotel_poi)

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

        mandatory_poi = {
            "id": poi_id,
            "name": poi.get("poi_name", "POI"),
            "coordinates": {
                "lat": poi.get("latitude"),
                "lng": poi.get("longitude"),
            },
            "roles": [poi.get("role", "attraction")],
            "area_name": poi.get("poi_destination"),
            "themes": poi.get("themes", []),
            "images": _sanitize_images(poi.get("images")),
        }
        if not any(p.get("id") == mandatory_poi["id"] for p in places):
            places.append(mandatory_poi)

    maut_output["places"] = places

    # Build user_input with hotels by city
    user_input = {}
    user_hotels_by_city = {}
    for hotel_data in payload.get("hotels", []):
        hotel_destination = hotel_data.get("destination")
        hotel_area_name = _normalize_destination_name(hotel_destination) if hotel_destination else None
        if hotel_area_name:
            user_hotels_by_city[hotel_area_name] = {
                "id": hotel_data.get("poi_id"),
                "name": hotel_data.get("poi_name", "Hotel"),
                "lat": hotel_data.get("latitude"),
                "lon": hotel_data.get("longitude"),
            }
    if user_hotels_by_city:
        user_input["user_hotels_by_city"] = user_hotels_by_city

    pipeline_output = run_full_pipeline(
        maut_output=maut_output,
        pacing=maut_request.get("pacing", "balanced"),
        mandatory=mandatory if mandatory else None,
        time_limit_sec=20,
        solver="acs",
        user_input=user_input if user_input else None,
    )

    if not pipeline_output.get("days") or pipeline_output.get("status") not in (
        "success",
        "partial_success",
    ):
        raise HTTPException(status_code=500, detail="Failed to optimize itinerary")

    data["plan"] = {
        "status": "ok",
        "days": pipeline_output.get("days", []),
        "items": [_transform_poi_to_frontend(p) for p in places],
        "meta": pipeline_output.get("meta", {}),
    }

    return data


def _recompute_partial(data: dict, options: dict) -> dict:
    """
    Partial recompute: Re-optimize using POIs currently in days.

    Extracts POIs from plan.days (not items bucket), deduplicates,
    and re-runs ACS solver while preserving city assignments.
    """
    plan = data.get("plan", {})
    days = plan.get("days", [])
    meta = data.get("meta", {})

    if not days:
        raise HTTPException(status_code=400, detail="No existing days to recompute")

    # Extract POIs from days
    places = _extract_pois_from_days(days)

    if not places:
        raise HTTPException(status_code=400, detail="No POIs found in days")

    # Group POIs by city/destination
    pois_by_city: dict[str, list] = {}
    for poi in places:
        city = poi.get("area_name") or "default"
        pois_by_city.setdefault(city, []).append(poi)

    # Get hotel info
    hotels_from_meta = meta.get("hotels", [])

    # Build mandatory constraints
    mandatory = {}
    for poi in meta.get("mandatory_pois", []):
        poi_id = poi.get("poi_id")
        if poi_id:
            mandatory[poi_id] = {
                "time_type": poi.get("time_type", "any_time"),
                "day": poi.get("day"),
            }

    pacing = options.get("pacing") or meta.get("preferences", {}).get("pacing", "balanced")

    # If single city, run simple pipeline
    if len(pois_by_city) == 1:
        city_name = list(pois_by_city.keys())[0]
        city_pois = pois_by_city[city_name]

        # Find hotel for this city
        hotel = None
        for h in hotels_from_meta:
            hotel = {
                "id": h.get("poi_id"),
                "name": h.get("poi_name", "Hotel"),
                "lat": h.get("latitude"),
                "lon": h.get("longitude"),
            }
            break

        if not hotel:
            # Use first accommodation from POIs
            for poi in city_pois:
                if "accommodation" in poi.get("roles", []):
                    coords = poi.get("coordinates", {})
                    hotel = {
                        "id": poi["id"],
                        "name": poi["name"],
                        "lat": coords.get("lat"),
                        "lon": coords.get("lng"),
                    }
                    break

        if not hotel:
            raise HTTPException(status_code=400, detail="No hotel found for recompute")

        maut_output = {
            "status": "ok",
            "places": city_pois,
            "meta": {
                "dates": meta.get("dates", {}),
                "num_days": len(days),
            },
        }

        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            pacing=pacing,
            mandatory=mandatory if mandatory else None,
            time_limit_sec=15,
            solver="acs",
        )
    else:
        # Multi-city: preserve city order from original days
        city_order = []
        city_days_count = {}

        for day in days:
            city = day.get("destination") or day.get("area_name") or "default"
            if city not in city_order:
                city_order.append(city)
            city_days_count[city] = city_days_count.get(city, 0) + 1

        # Build combined MAUT output
        all_places = []
        for city in city_order:
            for poi in pois_by_city.get(city, []):
                poi["area_name"] = city
                all_places.append(poi)

        maut_output = {
            "status": "ok",
            "places": all_places,
            "meta": {
                "dates": meta.get("dates", {}),
                "num_days": len(days),
            },
        }

        # Build user_input for city allocation
        user_input = {
            "days_per_city": city_days_count,
            "city_order": city_order,
        }

        # Build hotels by city
        user_hotels = {}
        for h in hotels_from_meta:
            dest = _normalize_destination_name(h.get("destination"))
            if dest:
                user_hotels[dest] = {
                    "id": h.get("poi_id"),
                    "name": h.get("poi_name", "Hotel"),
                    "lat": h.get("latitude"),
                    "lon": h.get("longitude"),
                }
        if user_hotels:
            user_input["user_hotels_by_city"] = user_hotels

        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            pacing=pacing,
            mandatory=mandatory if mandatory else None,
            time_limit_sec=15,
            solver="acs",
            user_input=user_input,
        )

    if not pipeline_output.get("days") or pipeline_output.get("status") not in (
        "success",
        "partial_success",
    ):
        raise HTTPException(status_code=500, detail="Failed to optimize itinerary")

    data["plan"]["days"] = pipeline_output.get("days", [])
    data["plan"]["meta"] = pipeline_output.get("meta", {})

    return data


def _recompute_single_day(data: dict, day_index: int, options: dict) -> dict:
    """
    Single-day recompute: Re-optimize a specific day using that day's POIs.

    Keeps other days unchanged, re-runs ACS for the specified day only.
    """
    plan = data.get("plan", {})
    days = plan.get("days", [])
    meta = data.get("meta", {})

    if day_index < 0 or day_index >= len(days):
        raise HTTPException(status_code=400, detail="Invalid day_index")

    target_day = days[day_index]

    # Extract POIs from this day only
    places = _extract_pois_from_single_day(target_day)

    if not places:
        raise HTTPException(status_code=400, detail="No POIs found in day")

    # Find hotel for this day
    depot_id = target_day.get("depot_id")
    hotel = None

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

    if not hotel:
        for stop in target_day.get("stops", []):
            if stop.get("role") == "accommodation":
                hotel = {
                    "id": stop.get("poi_id"),
                    "name": stop.get("name", "Hotel"),
                    "lat": stop.get("latitude"),
                    "lon": stop.get("longitude"),
                }
                break

    if not hotel and places:
        # Use first POI as reference point
        first_poi = places[0]
        coords = first_poi.get("coordinates", {})
        hotel = {
            "id": first_poi.get("id"),
            "name": first_poi.get("name", "Start Point"),
            "lat": coords.get("lat"),
            "lon": coords.get("lng"),
        }

    if not hotel:
        raise HTTPException(status_code=400, detail="No hotel or reference point found for day")

    dates_info = meta.get("dates", {})

    maut_output = {
        "status": "ok",
        "places": places,
        "meta": {
            "dates": dates_info,
            "num_days": 1,
        },
    }

    pacing = options.get("pacing") or meta.get("preferences", {}).get("pacing", "balanced")

    day_specs, nodes, travel = build_problem(
        maut_output,
        hotel,
        pacing=pacing,
        mandatory=None,
    )

    if not day_specs:
        raise HTTPException(status_code=400, detail="Failed to build problem for day")

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
        "images": _sanitize_images(poi.get("images")),
        "roles": poi.get("roles", []),
        "poiRoles": poi.get("roles", []),
        "themes": poi.get("themes", []),
        "description": poi.get("description"),
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("google_map_link"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "openHours": poi.get("open_hours"),
        "priceLevel": poi.get("price_level"),
    }
