import os
import json
import uuid
from fastapi import APIRouter, HTTPException
from app.services.transformers import (
    validate_create_itinerary_payload,
    transform_frontend_payload,
    transform_response_to_frontend,
    transform_poi_to_frontend,
)
from app.services.maut import run_pipeline
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["itinerary"])


# Storage Helpers


def get_storage_dir() -> str:
    """Get absolute path to itineraries storage directory."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "storage",
        "itineraries",
    )


def save_itinerary(itin_id: str, data: dict) -> None:
    """Persist itinerary to local JSON storage."""
    storage_dir = get_storage_dir()
    os.makedirs(storage_dir, exist_ok=True)

    storage_path = os.path.join(storage_dir, f"{itin_id}.json")
    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"Itinerary saved: {storage_path}")


def load_itinerary(itin_id: str) -> dict:
    """Load itinerary from local JSON storage."""
    storage_path = os.path.join(get_storage_dir(), f"{itin_id}.json")

    if not os.path.exists(storage_path):
        raise FileNotFoundError(f"Itinerary {itin_id} not found")

    with open(storage_path, "r", encoding="utf-8") as f:
        return json.load(f)


# API Endpoints


@router.post("/itinerary/create")
def create_itinerary(payload: dict):
    """
    Create a new itinerary from frontend form payload.

    Flow:
    1. Validate payload
    2. Transform frontend payload → MAUT request
    3. Run MAUT pipeline (fetch candidates, score, trim)
    4. Transform MAUT output → frontend plan
    5. Persist to storage
    6. Return response

    Args:
        payload: Frontend CreateItineraryPayload

    Returns:
        {
            "itin_id": str,
            "status": "success" | "error",
            "meta": {...},
            "plan": {
                "status": "ok",
                "items": POI[],
                "total_distance": float,
                "total_time": int,
                "route_order": str[],
                "selected_themes": str[]
            }
        }

    Raises:
        HTTPException: 400 for invalid payload, 500 for processing errors
    """
    itin_id = str(uuid.uuid4())

    try:
        # 1. Validate payload
        is_valid, error_msg = validate_create_itinerary_payload(payload)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)

        # 2. Transform frontend → MAUT request
        maut_request = transform_frontend_payload(payload)
        logger.info(
            f"MAUT request: destination={maut_request['destination']}, "
            f"num_days={maut_request['num_days']}, "
            f"flags={maut_request['flags']}"
        )

        # 3. Run MAUT pipeline
        maut_output = run_pipeline(maut_request)
        logger.info(f"MAUT output: {len(maut_output.get('places', []))} POIs selected")

        # 3.5. Enrich MAUT output with dates and num_days for CVRPTW compatibility
        maut_output["meta"]["dates"] = payload.get("dates", {})
        maut_output["meta"]["num_days"] = maut_request["num_days"]

        # 4. Extract hotel information from payload or MAUT output
        places = maut_output.get("places", [])

        # Check if hotels are provided in payload
        hotels_from_payload = payload.get("hotels", [])
        hotel = None

        if hotels_from_payload:
            # Use first hotel from payload
            first_hotel = hotels_from_payload[0]
            hotel = {
                "id": first_hotel.get("poi_id"),
                "name": first_hotel.get("poi_name", "Hotel"),
                "lat": first_hotel.get("latitude"),
                "lon": first_hotel.get("longitude"),
            }
            logger.info(f"Using hotel from payload: {hotel['name']}")
        else:
            # Fallback to MAUT-selected accommodation
            accommodations = [
                p for p in places if "accommodation" in p.get("poi_roles", [])
            ]
            if accommodations:
                hotel_poi = accommodations[0]
                coords = hotel_poi.get("coordinates") or {}
                hotel = {
                    "id": hotel_poi["id"],
                    "name": hotel_poi["name"],
                    "lat": coords.get("lat"),
                    "lon": coords.get("lng"),
                }
                logger.info(f"Using accommodation from MAUT: {hotel['name']}")

        # 5. Process mandatory POIs and hotels from payload and add to places
        mandatory_pois_from_payload = payload.get("mandatory_pois", [])
        mandatory = None

        # Add hotels to places if provided
        if hotels_from_payload:
            for hotel_data in hotels_from_payload:
                hotel_poi = {
                    "id": hotel_data.get("poi_id"),
                    "name": hotel_data.get("poi_name", "Hotel"),
                    "coordinates": {
                        "lat": hotel_data.get("latitude"),
                        "lng": hotel_data.get("longitude"),
                    },
                    "poi_roles": [hotel_data.get("role", "accommodation")],
                    "themes": hotel_data.get("themes", []),
                    "open_hours": hotel_data.get("open_hours"),
                    "images": hotel_data.get("images", []),
                }
                # Add to places if not already present
                if not any(p.get("id") == hotel_poi["id"] for p in places):
                    places.append(hotel_poi)
                    logger.info(f"Added hotel {hotel_poi['name']} to places")

        if mandatory_pois_from_payload:
            # Build mandatory dict for pipeline
            mandatory = {}
            for poi in mandatory_pois_from_payload:
                poi_id = poi.get("poi_id")
                if poi_id:
                    mandatory[poi_id] = {
                        "poi_id": poi_id,
                        "latitude": poi.get("latitude"),
                        "longitude": poi.get("longitude"),
                        "date": poi.get("date"),
                        "day": poi.get("day"),
                        "time_type": poi.get("time_type", "any_time"),
                        "start_time": poi.get("start_time"),
                        "end_time": poi.get("end_time"),
                        "themes": poi.get("themes", []),
                        "role": poi.get("role"),
                        "open_hours": poi.get("open_hours"),
                        "images": poi.get("images", []),
                    }

                    # Also add to places list for frontend display
                    mandatory_poi = {
                        "id": poi_id,
                        "name": poi.get("poi_name", "POI"),
                        "coordinates": {
                            "lat": poi.get("latitude"),
                            "lng": poi.get("longitude"),
                        },
                        "poi_roles": [poi.get("role", "attraction")],
                        "themes": poi.get("themes", []),
                        "open_hours": poi.get("open_hours"),
                        "images": poi.get("images", []),
                    }
                    # Add to places if not already present
                    if not any(p.get("id") == mandatory_poi["id"] for p in places):
                        places.append(mandatory_poi)
                        logger.info(
                            f"Added mandatory POI {mandatory_poi['name']} to places"
                        )

            logger.info(f"Processing {len(mandatory)} mandatory POIs from payload")

        # Update maut_output with enriched places
        maut_output["places"] = places

        # 6. Run full pipeline
        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            hotel=hotel,
            pacing=maut_request.get("pacing", "balanced"),
            mandatory=mandatory,
            time_limit_sec=20,
            solver="acs",
        )

        # 6. Transform pipeline output → frontend plan
        if pipeline_output.get("status") == "success":
            plan = {
                "status": "ok",
                "days": pipeline_output.get("days", []),
                "items": [transform_poi_to_frontend(p) for p in places],
                "meta": pipeline_output.get("meta", {}),
            }
        else:
            # Fallback to MAUT-only output if pipeline fails
            logger.warning("Pipeline failed, falling back to MAUT output")
            plan = transform_response_to_frontend(maut_output)
            plan["pipeline_error"] = pipeline_output.get("error")

        # 5. Build response
        result = {
            "itin_id": itin_id,
            "status": "success",
            "meta": {
                "title": payload.get("title"),
                "destination": maut_request["destination"],
                "dates": payload.get("dates", {}),
                "num_days": maut_request["num_days"],
                "travelers": payload.get("travelers", {}),
                "preferences": payload.get("preferences", {}),
                "dietary_restrictions": payload.get("dietary_restrictions"),
                "hotels": payload.get("hotels", []),
                "mandatory_pois": payload.get("mandatory_pois", []),
                "ideas": [],  # User-added POIs
            },
            "plan": plan,
        }

        # 6. Persist to storage
        save_itinerary(itin_id, result)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to create itinerary {itin_id}")
        raise HTTPException(
            status_code=500,
            detail={
                "itin_id": itin_id,
                "status": "error",
                "error": str(e),
                "message": "Failed to generate itinerary. Please try again.",
            },
        )


@router.get("/itinerary/{itin_id}")
def get_itinerary(itin_id: str):
    """
    Retrieve an existing itinerary by ID.

    Args:
        itin_id: Itinerary identifier

    Returns:
        Full itinerary data

    Raises:
        HTTPException: 404 if not found, 500 for errors
    """
    try:
        return load_itinerary(itin_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Itinerary not found")
    except Exception as e:
        logger.exception(f"Failed to load itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/itineraries")
def list_itineraries():
    """
    List all stored itineraries.

    Returns:
        List of itinerary metadata
    """
    try:
        storage_dir = get_storage_dir()
        if not os.path.exists(storage_dir):
            return []

        itineraries = []
        for filename in os.listdir(storage_dir):
            if filename.endswith(".json"):
                try:
                    itin_id = filename.replace(".json", "")
                    data = load_itinerary(itin_id)
                    itineraries.append(data)
                except Exception as e:
                    logger.warning(f"Failed to load itinerary {filename}: {e}")
                    continue

        return itineraries
    except Exception as e:
        logger.exception("Failed to list itineraries")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}")
def delete_itinerary(itin_id: str):
    """
    Delete an itinerary by ID.

    Args:
        itin_id: Itinerary identifier

    Returns:
        {"status": "deleted", "itin_id": str}

    Raises:
        HTTPException: 404 if not found, 500 for errors
    """
    try:
        storage_path = os.path.join(get_storage_dir(), f"{itin_id}.json")

        if not os.path.exists(storage_path):
            raise HTTPException(status_code=404, detail="Itinerary not found")

        os.remove(storage_path)
        logger.info(f"Deleted itinerary {itin_id}")

        return {"status": "deleted", "itin_id": itin_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/reorder")
def reorder_itinerary_stops(itin_id: str, payload: dict):
    """
    Reorder stops within a specific day.

    Args:
        itin_id: Itinerary identifier
        payload: {"day_index": int, "poi_ids": str[]}

    Returns:
        Updated itinerary data
    """
    try:
        day_index = payload.get("day_index")
        poi_ids = payload.get("poi_ids")

        if day_index is None or not isinstance(poi_ids, list):
            raise HTTPException(
                status_code=400, detail="day_index and poi_ids are required"
            )

        data = load_itinerary(itin_id)

        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        if day_index < 0 or day_index >= len(days):
            raise HTTPException(status_code=400, detail="Invalid day_index")

        day = days[day_index]
        stops = day.get("stops", [])

        # Reorder stops based on poi_ids
        stops_dict = {stop["poi_id"]: stop for stop in stops}
        new_stops = []
        for poi_id in poi_ids:
            if poi_id in stops_dict:
                new_stops.append(stops_dict[poi_id])

        # Add any stops not in poi_ids (shouldn't happen but safety)
        for stop in stops:
            if stop["poi_id"] not in poi_ids:
                new_stops.append(stop)

        day["stops"] = new_stops
        save_itinerary(itin_id, data)
        logger.info(f"Reordered day {day_index} in itinerary {itin_id}")

        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to reorder itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


def _time_to_minutes(t: str) -> int:
    """Convert 'HH:MM' to minutes since midnight. Returns large value on error."""
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        # Put invalid / missing times after valid ones
        return 24 * 60 + 1


def _sort_day_stops_by_time(day: dict) -> None:
    """
    Sort a day's stops by arrival time while keeping depot/hotel in place.

    Rules:
    - If first and/or last stop is depot/hotel, keep them fixed.
    - Among the remaining stops:
        - Timed stops (arrival set) ordered by arrival ascending.
        - Untimed/all-day stops go after timed stops (stable among themselves).
    """
    stops = day.get("stops", [])
    if not stops:
        return

    def is_depot(stop: dict) -> bool:
        role = stop.get("role")
        return role in ("depot", "hotel")

    def sort_key(stop: dict):
        arrival = stop.get("arrival")
        if arrival:
            return (0, _time_to_minutes(arrival))
        # all-day / no time
        return (1, _time_to_minutes("23:59"))

    # Common case: depot at start and/or end
    first_is_depot = is_depot(stops[0])
    last_is_depot = is_depot(stops[-1])

    if (
        first_is_depot
        and last_is_depot
        and len(stops) > 2
        and stops[0] is not stops[-1]
    ):
        middle = stops[1:-1]
        timed = [s for s in middle if s.get("arrival")]
        untimed = [s for s in middle if not s.get("arrival")]
        timed_sorted = sorted(timed, key=sort_key)
        day["stops"] = [stops[0], *timed_sorted, *untimed, stops[-1]]
    elif first_is_depot and len(stops) > 1:
        middle = stops[1:]
        timed = [s for s in middle if s.get("arrival")]
        untimed = [s for s in middle if not s.get("arrival")]
        timed_sorted = sorted(timed, key=sort_key)
        day["stops"] = [stops[0], *timed_sorted, *untimed]
    elif last_is_depot and len(stops) > 1:
        middle = stops[:-1]
        timed = [s for s in middle if s.get("arrival")]
        untimed = [s for s in middle if not s.get("arrival")]
        timed_sorted = sorted(timed, key=sort_key)
        day["stops"] = [*timed_sorted, *untimed, stops[-1]]
    else:
        timed = [s for s in stops if s.get("arrival")]
        untimed = [s for s in stops if not s.get("arrival")]
        timed_sorted = sorted(timed, key=sort_key)
        day["stops"] = [*timed_sorted, *untimed]


@router.post("/itinerary/{itin_id}/schedule-poi")
def schedule_poi(itin_id: str, payload: dict):
    """
    Update POI schedule (time or move to different day).

    Args:
        itin_id: Itinerary identifier
        payload: {
            "poi_id": str,
            "day_index": int,
            "start_time": str (optional, HH:MM format),
            "end_time": str (optional, HH:MM format),
            "all_day": bool (optional)
        }

    Returns:
        Updated itinerary data
    """
    try:
        poi_id = payload.get("poi_id")
        day_index = payload.get("day_index")

        if not poi_id or day_index is None:
            raise HTTPException(
                status_code=400, detail="poi_id and day_index are required"
            )

        data = load_itinerary(itin_id)

        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        if day_index < 0 or day_index >= len(days):
            raise HTTPException(status_code=400, detail="Invalid day_index")

        # Find and remove POI from current location (any day)
        poi_stop = None
        for day in days:
            stops = day.get("stops", [])
            for stop in stops:
                if stop.get("poi_id") == poi_id:
                    poi_stop = stop
                    day["stops"] = [s for s in stops if s.get("poi_id") != poi_id]
                    break
            if poi_stop:
                break

        if not poi_stop:
            raise HTTPException(status_code=404, detail="POI not found in itinerary")

        # Update times if provided
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        all_day = payload.get("all_day", False)

        if all_day:
            # All day: clear times
            poi_stop["arrival"] = None
            poi_stop["start_service"] = None
            poi_stop["depart"] = None
        elif start_time and end_time:
            poi_stop["arrival"] = start_time
            poi_stop["start_service"] = start_time
            poi_stop["depart"] = end_time

        # Add to target day
        target_day = days[day_index]
        target_day.setdefault("stops", [])
        target_day["stops"].append(poi_stop)

        # Enforce time-based ordering within the day
        _sort_day_stops_by_time(target_day)

        save_itinerary(itin_id, data)
        logger.info(f"Scheduled POI {poi_id} in itinerary {itin_id}")

        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to schedule POI in itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}/poi/{poi_id}")
def delete_poi_from_itinerary(itin_id: str, poi_id: str):
    """
    Remove a POI from the itinerary.

    Args:
        itin_id: Itinerary identifier
        poi_id: POI identifier to remove

    Returns:
        Updated itinerary data
    """
    try:
        data = load_itinerary(itin_id)

        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        # Remove from all days
        removed = False
        for day in data["plan"]["days"]:
            stops = day.get("stops", [])
            original_len = len(stops)
            day["stops"] = [s for s in stops if s["poi_id"] != poi_id]
            if len(day["stops"]) < original_len:
                removed = True

        if not removed:
            raise HTTPException(status_code=404, detail="POI not found in itinerary")

        save_itinerary(itin_id, data)
        logger.info(f"Deleted POI {poi_id} from itinerary {itin_id}")

        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete POI from itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/update-meta")
def update_itinerary_meta(itin_id: str, payload: dict):
    """
    Update itinerary metadata (dates, travelers, preferences, flags) and adjust plan days.

    Behavior:
    - If dates change and days increase: append empty days to plan.
    - If dates change and days decrease: move truncated day POIs to 'ideas' and trim days.

    Args:
        itin_id: Itinerary identifier
        payload: {
            "dates": {...},
            "travelers": {...},
            "preferences": {...},
            "flags": {...}
        }

    Returns:
        Updated itinerary data

    Raises:
        HTTPException: 404 if not found, 400 for invalid payload, 500 for errors
    """
    try:
        data = load_itinerary(itin_id)

        if "meta" not in data:
            data["meta"] = {}

        # Helpers
        def _compute_num_days(dates: dict) -> int | None:
            if not isinstance(dates, dict):
                return None
            t = dates.get("type")
            if t == "flexible":
                try:
                    d = int(dates.get("days") or 0)
                    return max(1, min(30, d)) if d > 0 else None
                except Exception:
                    return None
            if t == "specific":
                try:
                    from datetime import date as _date

                    s = _date.fromisoformat(str(dates.get("startDate")).split("T")[0])
                    e = _date.fromisoformat(str(dates.get("endDate")).split("T")[0])
                    return max(1, (e - s).days + 1)
                except Exception:
                    return None
            return None

        def _ensure_ideas(data_obj: dict):
            data_obj.setdefault("meta", {})
            data_obj["meta"].setdefault("ideas", [])

        # Apply meta updates
        if "dates" in payload:
            # Merge dates
            data["meta"]["dates"] = {
                **data["meta"].get("dates", {}),
                **payload["dates"],
            }

        if "travelers" in payload:
            data["meta"]["travelers"] = {
                **data["meta"].get("travelers", {}),
                **payload["travelers"],
            }

        if "preferences" in payload:
            data["meta"]["preferences"] = {
                **data["meta"].get("preferences", {}),
                **payload["preferences"],
            }

        if "flags" in payload:
            data["meta"]["flags"] = {
                **data["meta"].get("flags", {}),
                **payload["flags"],
            }

        # Adjust plan days if dates provided
        new_days = (
            _compute_num_days(payload.get("dates", {})) if "dates" in payload else None
        )

        # Ensure plan structure exists
        data.setdefault("plan", {})
        data["plan"].setdefault("days", [])
        days_list = data["plan"]["days"]
        current_days = len(days_list)

        if new_days and new_days != current_days:
            if new_days > current_days:
                # Append empty days
                for _ in range(new_days - current_days):
                    days_list.append({"dates": {}, "stops": []})
                logger.info(
                    f"Extended itinerary {itin_id} days: {current_days} -> {new_days}"
                )
            else:
                # Move POIs from truncated days to ideas
                _ensure_ideas(data)
                moved = 0
                from app.api.pois import get_poi_by_id

                truncated = days_list[new_days:]
                for day in truncated:
                    for stop in day.get("stops", []):
                        poi_id = stop.get("poi_id")
                        if not poi_id:
                            continue
                        try:
                            res = get_poi_by_id(poi_id)
                            if res and res.get("data"):
                                poi = res["data"]
                                # Avoid duplicates by id
                                existing_ids = [
                                    i.get("id") for i in data["meta"]["ideas"]
                                ]
                                if poi.get("id") not in existing_ids:
                                    data["meta"]["ideas"].append(
                                        {
                                            "id": poi.get("id"),
                                            "name": poi.get("name"),
                                            "category": poi.get("category"),
                                            "rating": poi.get("rating"),
                                            "location": poi.get("location"),
                                            "images": poi.get("images", []),
                                            "image": (
                                                poi.get("images", [None]) or [None]
                                            )[0],
                                        }
                                    )
                                    moved += 1
                        except Exception:
                            continue
                # Trim days
                data["plan"]["days"] = days_list[:new_days]
                # Add a notice for UI
                notices = data["meta"].get("notices", [])
                if moved:
                    notices.append(
                        f"{moved} POIs were moved to ideas due to reduced trip days"
                    )
                data["meta"]["notices"] = notices
                logger.info(
                    f"Trimmed itinerary {itin_id} days: {current_days} -> {new_days}, moved {moved} POIs to ideas"
                )

        # Persist updated itinerary
        save_itinerary(itin_id, data)
        logger.info(f"Updated metadata for itinerary {itin_id}")

        return data

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Itinerary not found")
    except Exception as e:
        logger.exception(f"Failed to update itinerary metadata {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/add-poi")
def add_poi_to_itinerary(itin_id: str, payload: dict):
    """
    Add a POI to an itinerary's ideas list.

    Args:
        itin_id: Itinerary identifier
        payload: {"poi_id": str, "day": int (optional)}

    Returns:
        Updated itinerary data

    Raises:
        HTTPException: 404 if itinerary not found, 400 for invalid payload, 500 for errors
    """
    try:
        # Validate payload
        poi_id = payload.get("poi_id")
        if not poi_id:
            raise HTTPException(status_code=400, detail="poi_id is required")

        # Load existing itinerary
        try:
            data = load_itinerary(itin_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Itinerary not found")

        # Initialize ideas array if needed
        if "meta" not in data:
            data["meta"] = {}
        if "ideas" not in data["meta"]:
            data["meta"]["ideas"] = []

        # Fetch POI details
        from app.api.pois import get_poi_by_id

        try:
            poi_response = get_poi_by_id(poi_id)
            if not poi_response or "data" not in poi_response:
                raise HTTPException(status_code=404, detail=f"POI {poi_id} not found")

            poi_details = poi_response["data"]

            # Check if POI already in ideas
            existing_ids = [item.get("id") for item in data["meta"]["ideas"]]
            if poi_id not in existing_ids:
                # Add POI to ideas
                idea_item = {
                    "id": poi_details.get("id"),
                    "name": poi_details.get("name"),
                    "category": poi_details.get("category"),
                    "rating": poi_details.get("rating"),
                    "location": poi_details.get("location"),
                    "images": poi_details.get("images", []),
                    "image": poi_details.get("images", [None])[0],
                }
                data["meta"]["ideas"].append(idea_item)

                # Save updated itinerary
                save_itinerary(itin_id, data)
                logger.info(f"Added POI {poi_id} to itinerary {itin_id}")
            else:
                logger.info(f"POI {poi_id} already in itinerary {itin_id}")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch POI details: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch POI details")

        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add POI to itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))
