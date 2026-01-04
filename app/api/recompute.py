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
from app.utils.naming import normalize_location_name
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.vrp_model import vrp_config

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["recompute"])


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
        day_area = day.get("destination") or day.get("area_name")

        for stop in day.get("stops", []):
            poi_id = stop.get("poi_id")
            if not poi_id:
                continue

            role = stop.get("role", "attraction")
            if role == "depot":
                continue

            # For hotel events, we still want to extract the accommodation POI
            # but only once (not for each checkin/checkout/stay event)
            hotel_event = stop.get("hotel_event_type")
            if hotel_event in ("checkin", "checkout", "stay"):
                # Use accommodation role for hotel POIs
                role = "accommodation"

            if poi_id in seen_ids:
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

            if day_area:
                poi["area_name"] = day_area

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
            city = normalize_location_name(raw_city)
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
        hotel_area_name = normalize_location_name(hotel_destination) if hotel_destination else None

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

        # Normalize poi_destination to match how segment_by_city normalizes area_name
        poi_destination = normalize_location_name(poi.get("poi_destination"))

        mandatory[poi_id] = {
            "time_type": poi.get("time_type", "any_time"),
            "day": poi.get("day"),
            "poi_destination": poi_destination,
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
            "area_name": poi_destination,
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
        hotel_area_name = normalize_location_name(hotel_destination) if hotel_destination else None
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

    POIs that don't fit within the time budget are moved to the ideas list.
    """
    plan = data.get("plan", {})
    days = plan.get("days", [])
    meta = data.get("meta", {})

    if not days:
        raise HTTPException(status_code=400, detail="No existing days to recompute")

    # Extract POIs from days - store original details for overflow handling
    places = _extract_pois_from_days(days)
    original_poi_details = {p["id"]: p.copy() for p in places}

    if not places:
        raise HTTPException(status_code=400, detail="No POIs found in days")

    pois_by_city: dict[str, list] = {}
    for poi in places:
        city = poi.get("area_name") or "default"
        pois_by_city.setdefault(city, []).append(poi)

    hotels_from_meta = meta.get("hotels", [])
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
        # No hotel - use first POI as reference (single-day without hotel)
        if not hotel and city_pois:
            first_poi = city_pois[0]
            coords = first_poi.get("coordinates", {})
            hotel = {
                "id": first_poi["id"],
                "name": first_poi["name"],
                "lat": coords.get("lat"),
                "lon": coords.get("lng"),
            }

        if not hotel:
            raise HTTPException(status_code=400, detail="No POIs found for recompute")

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
            dest = normalize_location_name(h.get("destination"))
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

    new_days = pipeline_output.get("days", [])
    data["plan"]["days"] = new_days
    data["plan"]["meta"] = pipeline_output.get("meta", {})

    # Detect overflow: POIs that were in the original days but couldn't be scheduled
    scheduled_poi_ids = set()
    for day in new_days:
        for stop in day.get("stops", []):
            poi_id = stop.get("poi_id", "")
            if poi_id:
                # Strip day suffix if present (e.g., "poi123_day0" -> "poi123")
                base_id = poi_id.rsplit("_day", 1)[0]
                scheduled_poi_ids.add(base_id)
                scheduled_poi_ids.add(poi_id)

    # Find unscheduled POIs and add them to ideas
    original_poi_ids = set(original_poi_details.keys())
    unscheduled_ids = original_poi_ids - scheduled_poi_ids

    if unscheduled_ids:
        data.setdefault("meta", {}).setdefault("ideas", [])
        existing_idea_ids = {i.get("id") for i in data["meta"]["ideas"]}

        for poi_id in unscheduled_ids:
            if poi_id not in existing_idea_ids:
                poi_detail = original_poi_details.get(poi_id, {})
                data["meta"]["ideas"].append(
                    {
                        "id": poi_id,
                        "name": poi_detail.get("name", "Unknown"),
                        "category": None,
                        "categories": [],
                        "rating": None,
                        "reviews_count": None,
                        "roles": poi_detail.get("roles", ["attraction"]),
                        "role": poi_detail.get("roles", ["attraction"])[0] if poi_detail.get("roles") else "attraction",
                        "themes": poi_detail.get("themes", []),
                        "location": poi_detail.get("area_name"),
                        "images": poi_detail.get("images", []),
                        "coordinates": poi_detail.get("coordinates"),
                        "reason_not_scheduled": "Could not fit within trip's time budget",
                    }
                )

        logger.info(f"Moved {len(unscheduled_ids)} overflow POIs to ideas during partial recompute")

    # Also add mandatory POIs that couldn't be scheduled (from pipeline meta)
    if pipeline_output.get("meta", {}).get("mandatory_ideas"):
        data.setdefault("meta", {}).setdefault("ideas", [])
        existing_idea_ids = {i.get("id") for i in data["meta"]["ideas"]}
        for idea in pipeline_output["meta"]["mandatory_ideas"]:
            if idea.get("id") not in existing_idea_ids:
                data["meta"]["ideas"].append(idea)

    return data


def _recompute_single_day(data: dict, day_index: int, options: dict) -> dict:
    """
    Single-day recompute: Re-optimize a specific day with hotel events in solver.

    Hotel events (checkout/checkin/stay) are included as nodes so the solver
    optimizes the full route including travel to/from hotels.

    POIs that don't fit within the time budget are moved to the ideas list.
    """
    import datetime as dt
    from app.services.vrp_model import DaySpec, HotelEvent, HotelEventType
    from app.services.vrp_utils import create_nodes
    from app.services.osrm import osrm_client

    plan = data.get("plan", {})
    days = plan.get("days", [])
    meta = data.get("meta", {})

    if day_index < 0 or day_index >= len(days):
        raise HTTPException(status_code=400, detail="Invalid day_index")

    target_day = days[day_index]

    # Extract POIs and build hotel events for solver
    seen_ids = set()
    places = []
    hotel_events = []
    depot_hotel = None
    original_poi_details = {}

    for stop in target_day.get("stops", []):
        poi_id = stop.get("poi_id")
        if not poi_id or stop.get("role") == "depot":
            continue

        coords = stop.get("coordinates") or {}
        lat = coords.get("lat") or stop.get("latitude")
        lng = coords.get("lng") or stop.get("longitude")
        if not lat or not lng:
            continue

        event_type = stop.get("hotel_event_type")
        if event_type == "checkout":
            hotel_events.append(
                HotelEvent(
                    event_type=HotelEventType.CHECK_OUT,
                    hotel_id=str(poi_id),
                    hotel_name=stop.get("name", "Hotel"),
                    lat=float(lat),
                    lon=float(lng),
                    window=vrp_config.hotel_check_out_window,
                    service_time=vrp_config.hotel_service_time,
                )
            )
            depot_hotel = {"id": poi_id, "name": stop.get("name"), "lat": lat, "lon": lng}
        elif event_type == "checkin":
            hotel_events.append(
                HotelEvent(
                    event_type=HotelEventType.CHECK_IN,
                    hotel_id=str(poi_id),
                    hotel_name=stop.get("name", "Hotel"),
                    lat=float(lat),
                    lon=float(lng),
                    window=vrp_config.hotel_check_in_window,
                    service_time=vrp_config.hotel_service_time,
                )
            )
        elif event_type == "stay":
            hotel_events.append(
                HotelEvent(
                    event_type=HotelEventType.STAY,
                    hotel_id=str(poi_id),
                    hotel_name=stop.get("name", "Hotel"),
                    lat=float(lat),
                    lon=float(lng),
                    window=(0, 24 * 60),
                    service_time=0,
                )
            )
            if not depot_hotel:
                depot_hotel = {"id": poi_id, "name": stop.get("name"), "lat": lat, "lon": lng}
        else:
            if poi_id in seen_ids:
                continue
            seen_ids.add(poi_id)
            places.append(
                {
                    "id": poi_id,
                    "name": stop.get("name", "Unknown"),
                    "coordinates": {"lat": lat, "lng": lng},
                    "roles": [stop.get("role", "attraction")],
                    "themes": stop.get("themes", []),
                    "open_hours": stop.get("open_hours"),
                    "images": _sanitize_images(stop.get("images")),
                }
            )
            # Store original stop details for potential overflow
            original_poi_details[poi_id] = {
                "id": poi_id,
                "name": stop.get("name", "Unknown"),
                "category": stop.get("category"),
                "categories": stop.get("categories", []),
                "rating": stop.get("rating"),
                "reviews_count": stop.get("reviews_count"),
                "roles": [stop.get("role", "attraction")],
                "role": stop.get("role", "attraction"),
                "themes": stop.get("themes", []),
                "location": target_day.get("destination"),
                "images": _sanitize_images(stop.get("images")),
                "coordinates": {"lat": lat, "lng": lng},
            }

    if not places:
        raise HTTPException(status_code=400, detail="No POIs found in day")

    # Find depot from meta if not from hotel events
    if not depot_hotel:
        for h in meta.get("hotels", []):
            depot_hotel = {
                "id": h.get("poi_id"),
                "name": h.get("poi_name", "Hotel"),
                "lat": h.get("latitude"),
                "lon": h.get("longitude"),
            }
            break
    if not depot_hotel:
        c = places[0].get("coordinates", {})
        depot_hotel = {"id": places[0]["id"], "name": places[0]["name"], "lat": c.get("lat"), "lon": c.get("lng")}

    # Build day spec with hotel events
    pacing = options.get("pacing") or meta.get("preferences", {}).get("pacing", "balanced")
    start_min = vrp_config.pace_day_start_min.get(pacing, 9 * 60)
    end_min = start_min + vrp_config.pace_day_budget_min.get(pacing, 11 * 60)

    day_date = dt.date.today()
    if target_day.get("date"):
        try:
            day_date = dt.date.fromisoformat(str(target_day["date"]).split("T")[0])
        except (ValueError, TypeError):
            pass

    day_spec = DaySpec(
        day_index=0,
        date=day_date,
        start_min=start_min,
        end_min=end_min,
        depot_id=str(depot_hotel["id"]),
        hotel_events=hotel_events,
    )

    maut_output = {"status": "ok", "places": places, "meta": {"num_days": 1}}
    nodes = create_nodes(maut_output, [day_spec], depot_hotel, pacing)
    coords = [(n.lat, n.lon) for n in nodes]
    travel = osrm_client.matrix_minutes(coords)

    result = run_acs_cvrptw(
        day_specs=[day_spec],
        nodes=nodes,
        travel=travel,
        meals_required=options.get("meals_required", 3),
        cfg=vrp_config,
    )

    if not result.get("days"):
        raise HTTPException(status_code=500, detail="Failed to optimize day")

    new_day = result["days"][0]
    for key in ("date", "weekday", "area_name", "destination", "depot_id", "source"):
        new_day[key] = target_day.get(key)
    new_day["optimization_method"] = "acs_cvrptw"

    # Detect overflow: POIs that were in the day but couldn't be scheduled
    scheduled_poi_ids = set()
    for stop in new_day.get("stops", []):
        poi_id = stop.get("poi_id", "")
        if poi_id:
            # Strip day suffix if present (e.g., "poi123_day0" -> "poi123")
            base_id = poi_id.rsplit("_day", 1)[0]
            scheduled_poi_ids.add(base_id)
            scheduled_poi_ids.add(poi_id)  # Also add original

    # Find unscheduled POIs and add them to ideas
    original_poi_ids = set(original_poi_details.keys())
    unscheduled_ids = original_poi_ids - scheduled_poi_ids

    if unscheduled_ids:
        data.setdefault("meta", {}).setdefault("ideas", [])
        existing_idea_ids = {i.get("id") for i in data["meta"]["ideas"]}

        for poi_id in unscheduled_ids:
            if poi_id not in existing_idea_ids:
                poi_detail = original_poi_details.get(poi_id, {})
                data["meta"]["ideas"].append(
                    {
                        "id": poi_id,
                        "name": poi_detail.get("name", "Unknown"),
                        "category": poi_detail.get("category"),
                        "categories": poi_detail.get("categories", []),
                        "rating": poi_detail.get("rating"),
                        "reviews_count": poi_detail.get("reviews_count"),
                        "roles": poi_detail.get("roles", ["attraction"]),
                        "role": poi_detail.get("role", "attraction"),
                        "themes": poi_detail.get("themes", []),
                        "location": poi_detail.get("location"),
                        "images": poi_detail.get("images", []),
                        "coordinates": poi_detail.get("coordinates"),
                        "reason_not_scheduled": "Could not fit within day's time budget",
                    }
                )

        logger.info(f"Moved {len(unscheduled_ids)} overflow POIs to ideas for day {day_index}")

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
