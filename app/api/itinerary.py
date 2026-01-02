import uuid
from typing import Optional
from fastapi import APIRouter, HTTPException, Header
from app.services.transformers import (
    transform_frontend_payload,
    transform_poi_to_frontend,
    transform_itinerary_response_to_frontend,
)
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger
from app.services.vrp_model import vrp_config
from app.services.osrm import osrm_client, tiered_round
from app.utils.naming import transform_frontend_to_canonical
from app.utils.date_utils import recompute_day_labels, time_to_minutes, compute_num_days
from app.db.itinerary_storage import (
    save_itinerary_to_db,
    load_itinerary_from_db,
    soft_delete_itinerary_for_user,
    list_itineraries_from_db,
    load_itinerary_for_user,
    update_itinerary_plan_for_user,
    update_itinerary_meta_for_user,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["itinerary"])

# Helpers


def _normalize_destination_name(raw) -> Optional[str]:
    """Normalize location label to city name. Example: 'Johor, Malaysia' -> 'Johor'."""
    if not raw:
        return None
    name = str(raw).strip()
    return name.split(",")[0].strip() if "," in name else name


def get_optional_user_id(authorization: Optional[str]) -> Optional[str]:
    """Extract user ID from Bearer token if valid, None otherwise."""
    if not authorization:
        return None
    try:
        from app.api.auth import get_user_from_token

        token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
        user = get_user_from_token(token)
        return user["id"] if user else None
    except Exception:
        return None


def save_itinerary(itin_id: str, data: dict, user_id: Optional[str] = None) -> None:
    """Persist itinerary to database."""
    if user_id:
        data.setdefault("meta", {})["user_id"] = user_id
    if not save_itinerary_to_db(itin_id, data):
        logger.warning(f"Database save failed for {itin_id}")


def load_itinerary_with_auth(itin_id: str, user_id: Optional[str]) -> dict:
    """
    Load itinerary with ownership verification via RPC.

    Raises:
        HTTPException: 403 if not owner, 404 if not found
    """
    data, error = load_itinerary_for_user(itin_id, user_id)
    if error == "forbidden":
        raise HTTPException(status_code=403, detail="You don't have permission to access this itinerary")
    if error == "not_found" or not data:
        raise HTTPException(status_code=404, detail="Itinerary not found")
    return data


def _handle_storage_error(error: str, action: str = "modify") -> None:
    """Raise appropriate HTTPException based on storage error code."""
    if error == "forbidden":
        raise HTTPException(status_code=403, detail=f"You don't have permission to {action} this itinerary")
    if error == "not_found":
        raise HTTPException(status_code=404, detail="Itinerary not found")
    raise HTTPException(status_code=500, detail=f"Failed to {action} itinerary: {error}")


# Day/Stop Utilities


def _recompute_day_metrics(day: dict) -> None:
    """Recompute day's total distance using OSRM."""
    stops = day.get("stops", [])
    total = 0.0
    for i in range(len(stops) - 1):
        c1 = stops[i].get("coordinates", {})
        c2 = stops[i + 1].get("coordinates", {})
        lat1, lon1 = c1.get("lat") or stops[i].get("latitude"), c1.get("lng") or stops[i].get("longitude")
        lat2, lon2 = c2.get("lat") or stops[i + 1].get("latitude"), c2.get("lng") or stops[i + 1].get("longitude")
        if None not in (lat1, lon1, lat2, lon2):
            try:
                total += osrm_client.distance(lat1, lon1, lat2, lon2)
            except Exception:
                pass
    day["total_distance"] = round(total, 2)


def _recalculate_day_times(day: dict, pacing: str = "balanced") -> None:
    """Recalculate arrival/depart times for all stops using OSRM travel times."""
    stops = day.get("stops", [])
    if not stops:
        return

    service_times = vrp_config.service_time_min

    def get_duration(stop: dict) -> int:
        custom = stop.get("duration_min") or stop.get("service_time")
        if custom:
            try:
                return int(custom)
            except (ValueError, TypeError):
                pass
        return service_times.get(stop.get("role", "attraction"), {}).get(pacing, 60)

    def get_coords(stop: dict) -> tuple:
        c = stop.get("coordinates", {})
        return c.get("lat") or stop.get("latitude"), c.get("lng") or stop.get("longitude")

    def to_time(mins: int) -> str:
        mins = max(0, min(mins, 24 * 60 - 1))
        return f"{mins // 60:02d}:{mins % 60:02d}"

    first = stops[0]
    current = None
    if first.get("arrival"):
        try:
            h, m = map(int, str(first["arrival"]).split(":"))
            current = h * 60 + m
        except (ValueError, TypeError):
            pass
    if current is None:
        current = vrp_config.pace_day_start_min.get(pacing, 9 * 60)

    for i, stop in enumerate(stops):
        role = stop.get("role", "attraction")
        is_depot = role in ("accommodation")

        if i == 0 and is_depot:
            stop["arrival"] = stop["depart"] = to_time(current)
            continue

        if i > 0:
            lat1, lon1 = get_coords(stops[i - 1])
            lat2, lon2 = get_coords(stop)
            if None not in (lat1, lon1, lat2, lon2):
                try:
                    travel_sec = osrm_client.route(lat1, lon1, lat2, lon2)
                    current += tiered_round(travel_sec / 60.0)
                except Exception:
                    current += 15
            else:
                current += 15

        if i == len(stops) - 1 and is_depot:
            stop["arrival"] = to_time(current)
            stop["depart"] = None
            continue

        stop["arrival"] = to_time(current)
        current += get_duration(stop)
        stop["depart"] = to_time(current)


def _sort_day_stops_by_time(day: dict) -> None:
    """Sort stops by arrival time, keeping depot/hotel at start/end."""
    stops = day.get("stops", [])
    if not stops:
        return

    def is_depot(s: dict) -> bool:
        return s.get("role") in ("depot", "hotel", "accommodation")

    def sort_key(s: dict):
        arr = s.get("arrival")
        if arr:
            return (0, time_to_minutes(arr, default=24 * 60 + 1))
        return (1, 24 * 60 + 1)

    first_depot = is_depot(stops[0])
    last_depot = is_depot(stops[-1]) if len(stops) > 1 else False

    if first_depot and last_depot and stops[0] is not stops[-1]:
        middle = stops[1:-1]
        timed = sorted([s for s in middle if s.get("arrival")], key=sort_key)
        untimed = [s for s in middle if not s.get("arrival")]
        day["stops"] = [stops[0], *timed, *untimed, stops[-1]]
    elif last_depot:
        middle = stops[:-1]
        timed = sorted([s for s in middle if s.get("arrival")], key=sort_key)
        untimed = [s for s in middle if not s.get("arrival")]
        day["stops"] = [*timed, *untimed, stops[-1]]
    else:
        timed = sorted([s for s in stops if s.get("arrival")], key=sort_key)
        untimed = [s for s in stops if not s.get("arrival")]
        day["stops"] = [*timed, *untimed]


def _extract_core_stops(stops: list) -> tuple[list, list, list]:
    """Extract prefix (first depot), core stops, suffix (last depot)."""
    if not stops:
        return [], [], []

    def is_depot(s: dict) -> bool:
        return s.get("role") in ("hotel", "accommodation") or s.get("hotel_event_type") in (
            "checkin",
            "checkout",
            "stay",
        )

    first = stops[0] if stops and is_depot(stops[0]) else None
    last = stops[-1] if len(stops) > 1 and is_depot(stops[-1]) and stops[-1] is not first else None

    start_idx = 1 if first else 0
    end_idx = -1 if last else len(stops)
    core = stops[start_idx:end_idx] if end_idx != len(stops) else stops[start_idx:]

    return [first] if first else [], core, [last] if last else []


# API Endpoints


@router.post("/itinerary/create")
def create_itinerary(payload: dict, authorization: Optional[str] = Header(None)):
    """Create a new itinerary from frontend form payload."""
    itin_id = str(uuid.uuid4())
    user_id = get_optional_user_id(authorization)

    try:
        try:
            payload = transform_frontend_to_canonical(payload)
            print(payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        maut_request = transform_frontend_payload(payload)
        destinations = payload.get("destinations") if isinstance(payload.get("destinations"), list) else []

        if destinations:
            maut_output = _run_multi_city_maut(maut_request, destinations)
        else:
            maut_output = run_maut(maut_request)

        maut_output.setdefault("meta", {})
        maut_output["meta"]["dates"] = payload.get("dates", {})
        maut_output["meta"]["num_days"] = maut_request["num_days"]

        places = maut_output.get("places", [])
        hotels_from_payload = payload.get("hotels", [])
        mandatory_pois_from_payload = payload.get("mandatory_pois", [])

        _add_hotels_to_places(places, hotels_from_payload)
        mandatory = _process_mandatory_pois(places, mandatory_pois_from_payload, payload.get("dates", {}))
        maut_output["places"] = places

        user_input = _build_user_input(places, hotels_from_payload, destinations, payload, maut_request)

        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            pacing=maut_request.get("pacing", "balanced"),
            mandatory=mandatory,
            time_limit_sec=20,
            solver="acs",
            user_input=user_input,
        )

        if pipeline_output.get("status") == "success" and len(destinations) > 1:
            _validate_multi_city_output(pipeline_output, destinations)

        if pipeline_output.get("status") == "success":
            plan = {
                "status": "ok",
                "days": pipeline_output.get("days", []),
                "items": [transform_poi_to_frontend(p) for p in places],
                "meta": pipeline_output.get("meta", {}),
            }
        else:
            logger.warning("Pipeline failed")
            plan = {"status": "error", "days": [], "items": [], "meta": {}}
            plan["pipeline_error"] = pipeline_output.get("error")

        pipeline_meta = pipeline_output.get("meta", {}) if pipeline_output else {}
        result = {
            "itin_id": itin_id,
            "status": "success",
            "meta": {
                "title": payload.get("title"),
                "destinations": destinations or [],
                "dates": payload.get("dates", {}),
                "num_days": maut_request["num_days"],
                "travelers": payload.get("travelers", {}),
                "preferences": payload.get("preferences", {}),
                "dietary_restrictions": payload.get("dietary_restrictions"),
                "hotels": hotels_from_payload,
                "mandatory_pois": mandatory_pois_from_payload,
                "ideas": pipeline_meta.get("mandatory_ideas", []),
            },
            "plan": plan,
        }

        save_itinerary(itin_id, result, user_id)
        return transform_itinerary_response_to_frontend(result)

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


def _run_multi_city_maut(maut_request: dict, destinations: list) -> dict:
    """Run MAUT for multiple destinations and combine results."""
    all_places = []
    selected_themes: list[str] = []

    def city_variants(name: str) -> list[str]:
        return [name, "Johor Bahru"] if name.lower() == "johor" else [name]

    for d in destinations:
        city = _normalize_destination_name(d.get("city"))
        if not city:
            continue

        out = None
        for cname in city_variants(city):
            req = dict(maut_request)
            req["destination"] = cname
            out = run_maut(req)
            if out.get("places"):
                break

        if not out:
            continue

        for p in out.get("places", []) or []:
            p["requested_city"] = city
            all_places.append(p)

        for t in out.get("meta", {}).get("selected_themes", []):
            if t not in selected_themes:
                selected_themes.append(t)

    return {
        "status": "ok",
        "places": all_places,
        "total_distance": 0.0,
        "total_time": 0,
        "route_order": [],
        "meta": {"selected_themes": selected_themes[:3] or maut_request.get("interest_themes", [])[:3]},
    }


def _extract_hotel(places: list, hotels_from_payload: list, is_multi_city: bool) -> Optional[dict]:
    """Extract hotel for single-city itinerary."""
    if is_multi_city:
        return None

    if hotels_from_payload:
        h = hotels_from_payload[0]
        return {
            "id": h.get("poi_id"),
            "name": h.get("poi_name", "Hotel"),
            "lat": h.get("latitude"),
            "lon": h.get("longitude"),
        }

    accommodations = [p for p in places if "accommodation" in p.get("roles", [])]
    if accommodations:
        h = accommodations[0]
        coords = h.get("coordinates") or {}
        return {"id": h["id"], "name": h["name"], "lat": coords.get("lat"), "lon": coords.get("lng")}

    return None


def _add_hotels_to_places(places: list, hotels: list) -> None:
    """Add hotel POIs to places list."""
    for h in hotels:
        area = _normalize_destination_name(h.get("destination"))
        poi = {
            "id": h.get("poi_id"),
            "name": h.get("poi_name", "Hotel"),
            "coordinates": {"lat": h.get("latitude"), "lng": h.get("longitude")},
            "roles": [h.get("role", "accommodation")],
            "themes": h.get("themes", []),
            "open_hours": h.get("open_hours"),
            "images": h.get("images", []),
            "source": "user",
        }
        if area:
            poi["area_name"] = area
        if not any(p.get("id") == poi["id"] for p in places):
            places.append(poi)


def _process_mandatory_pois(places: list, mandatory_pois: list, dates_info: dict) -> Optional[dict]:
    """Process mandatory POIs and add to places."""
    if not mandatory_pois:
        return None

    mandatory = {}
    is_specific = dates_info.get("type") == "specific"

    for poi in mandatory_pois:
        poi_id = poi.get("poi_id")
        if not poi_id:
            continue

        time_type = poi.get("time_type", "any_time")
        entry = {
            "time_type": "all_day" if time_type in ("all_day", "allDay") else time_type,
            "poi_name": poi.get("poi_name", "Unknown POI"),
            "role": poi.get("role", "attraction"),
            "themes": poi.get("themes", []),
            "images": poi.get("images", []),
        }

        dest = _normalize_destination_name(poi.get("poi_destination"))
        if dest:
            entry["poi_destination"] = dest

        if is_specific and poi.get("date"):
            try:
                from datetime import date as _date

                start = dates_info.get("start_date")
                if start:
                    trip_start = _date.fromisoformat(str(start).split("T")[0])
                    poi_date = _date.fromisoformat(str(poi["date"]).split("T")[0])
                    day_idx = (poi_date - trip_start).days + 1
                    if day_idx > 0:
                        entry["day"] = day_idx
            except Exception:
                pass
        elif isinstance(poi.get("day"), int) and poi["day"] > 0:
            entry["day"] = poi["day"]

        if entry["time_type"] == "all_day":
            entry["all_day"] = True
        elif time_type == "specific" and poi.get("start_time") and poi.get("end_time"):
            entry["window"] = [poi["start_time"], poi["end_time"]]

        mandatory[poi_id] = entry

        poi_data = {
            "id": poi_id,
            "name": poi.get("poi_name", "POI"),
            "coordinates": {"lat": poi.get("latitude"), "lng": poi.get("longitude")},
            "roles": [poi.get("role", "attraction")],
            "area_name": poi.get("poi_destination"),
            "themes": poi.get("themes", []),
            "open_hours": poi.get("open_hours"),
            "images": poi.get("images", []),
        }
        if not any(p.get("id") == poi_id for p in places):
            places.append(poi_data)

    return mandatory


def _build_user_input(
    places: list, hotels: list, destinations: list, payload: dict, maut_request: dict
) -> Optional[dict]:
    """Build user_input dict for pipeline."""
    user_input = None

    if hotels:
        user_hotels = {}
        place_cities = [(p, p.get("area_name")) for p in places]
        for h in hotels:
            hlat, hlon = h.get("latitude"), h.get("longitude")
            if hlat is None or hlon is None:
                continue
            best_city, best_d = None, None
            for p, cname in place_cities:
                if not cname:
                    continue
                coords = p.get("coordinates") or {}
                plat, plon = coords.get("lat"), coords.get("lng")
                if plat is None or plon is None:
                    continue
                d = (float(plat) - float(hlat)) ** 2 + (float(plon) - float(hlon)) ** 2
                if best_d is None or d < best_d:
                    best_d, best_city = d, cname
            if best_city:
                user_hotels[best_city] = {
                    "id": h.get("poi_id"),
                    "name": h.get("poi_name", "Hotel"),
                    "lat": hlat,
                    "lon": hlon,
                    "source": "user",
                }
        if user_hotels:
            user_input = {"user_hotels_by_city": user_hotels}

    if destinations:
        total_days = compute_num_days(payload.get("dates", {})) or maut_request["num_days"]
        days_per_city, ordered_cities, per_city_dates = _compute_days_per_city(destinations, total_days)

        if days_per_city:
            user_input = user_input or {}
            user_input["days_per_city"] = days_per_city
            user_input["city_order"] = ordered_cities
        if per_city_dates:
            user_input = user_input or {}
            user_input["per_city_dates"] = per_city_dates

    return user_input


def _compute_days_per_city(destinations: list, total_days: int) -> tuple[dict, list, dict]:
    """Compute days allocation per city."""
    provided = {}
    ordered = []
    per_city_dates = {}

    for d in destinations:
        city = _normalize_destination_name(d.get("city"))
        if not city:
            continue
        ordered.append(city)

        if d.get("days") is not None:
            try:
                provided[city] = max(1, int(d["days"]))
            except Exception:
                provided[city] = 0
        elif isinstance(d.get("dates"), dict):
            dd = compute_num_days(d["dates"])
            if dd:
                provided[city] = dd
                per_city_dates[city] = {
                    "type": "specific",
                    "start_date": d["dates"].get("start_date"),
                    "end_date": d["dates"].get("end_date"),
                }

    if not provided:
        k = len(ordered)
        if k > 0:
            q, r = divmod(total_days, k)
            return {c: q + (1 if i < r else 0) for i, c in enumerate(ordered)}, ordered, per_city_dates

    s = sum(max(0, v) for v in provided.values())
    if s > 0 and s != total_days:
        ratio = total_days / s
        base = {k: max(0, int(round(v * ratio))) for k, v in provided.items()}
        diff = total_days - sum(base.values())
        for k in ordered:
            if k in base and diff != 0:
                base[k] += 1 if diff > 0 else -1
                diff -= 1 if diff > 0 else -1
                if diff == 0:
                    break
        return base, ordered, per_city_dates

    return {k: max(1, int(v)) for k, v in provided.items()}, ordered, per_city_dates


def _validate_multi_city_output(pipeline_output: dict, destinations: list) -> None:
    """
    Validate multi-city output has all expected cities.

    If some cities are missing, log a warning but don't fail - the itinerary
    is still usable. This can happen when:
    - A city has no POIs matching user preferences
    - Day allocation couldn't fit all cities
    - Mandatory POIs forced all days to one city
    """
    expected = [_normalize_destination_name(d.get("city")) for d in destinations if d.get("city")]
    days_out = pipeline_output.get("days", []) or []
    actual = sorted({str(d.get("destination") or d.get("area_name") or "").strip() for d in days_out} - {""})

    if len(expected) > 1 and len(actual) < len(expected):
        missing = [c for c in expected if c not in actual]
        logger.warning(
            f"multi_city_partial: expected={expected} actual={actual} missing={missing}. "
            "Some cities may not have enough days or POIs."
        )
        # Add warning to meta instead of failing
        pipeline_output.setdefault("meta", {})["multi_city_warning"] = {
            "message": f"Some destinations could not be included: {', '.join(missing)}",
            "expected_cities": expected,
            "actual_cities": actual,
            "missing_cities": missing,
        }


@router.get("/itinerary/{itin_id}")
def get_itinerary(itin_id: str, authorization: Optional[str] = Header(None)):
    """Retrieve an itinerary by ID with ownership verification."""
    try:
        user_id = get_optional_user_id(authorization)
        data = load_itinerary_with_auth(itin_id, user_id)
        return transform_itinerary_response_to_frontend(data)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to load itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/itineraries")
def list_itineraries(authorization: Optional[str] = Header(None)):
    """List itineraries for authenticated user."""
    try:
        user_id = get_optional_user_id(authorization)
        if not user_id:
            return []

        db_itineraries, _ = list_itineraries_from_db(user_id)
        result = []
        for itin in db_itineraries:
            full = load_itinerary_from_db(itin.get("id"))
            if full:
                result.append(transform_itinerary_response_to_frontend(full))
        return result
    except Exception as e:
        logger.exception("Failed to list itineraries")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}")
def delete_itinerary(itin_id: str, authorization: Optional[str] = Header(None)):
    """Soft-delete an itinerary."""
    try:
        user_id = get_optional_user_id(authorization)
        success, error = soft_delete_itinerary_for_user(itin_id, user_id)

        if not success:
            _handle_storage_error(error, "delete")

        return {"status": "deleted", "itinId": itin_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/reorder")
def reorder_itinerary_stops(itin_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    """Reorder itinerary stops (single-day or entire-trip scope)."""
    try:
        user_id = get_optional_user_id(authorization)
        payload = transform_frontend_to_canonical(payload)

        scope = payload.get("scope") or "single_day"
        ordered = payload.get("ordered_poi_ids") or payload.get("poi_ids") or []
        moves = payload.get("moves") or {}
        target_positions = payload.get("target_positions") or {}
        recalculate_times = payload.get("recalculate_times", True)
        options = payload.get("options") or {}

        if not isinstance(ordered, list):
            raise HTTPException(status_code=400, detail="ordered_poi_ids must be a list")

        data = load_itinerary_with_auth(itin_id, user_id)
        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        pacing = data.get("meta", {}).get("preferences", {}).get("pacing", "balanced")
        affected_days = set()

        if scope == "single_day":
            day_index = payload.get("day_index")
            if day_index is None or not (0 <= int(day_index) < len(days)):
                raise HTTPException(status_code=400, detail="day_index is required and must be valid")

            day_index = int(day_index)
            day = days[day_index]
            prefix, core, suffix = _extract_core_stops(day.get("stops", []))

            by_id = {s["poi_id"]: s for s in core}
            new_core = [by_id[i] for i in ordered if i in by_id]
            for s in core:
                if s["poi_id"] not in ordered:
                    new_core.append(s)

            day["stops"] = prefix + new_core + suffix
            affected_days.add(day_index)
            _recompute_day_metrics(day)

        elif scope == "entire_trip":
            # Apply cross-day moves
            if moves:
                idx_map = {}
                for d_i, d in enumerate(days):
                    for s in d.get("stops", []):
                        idx_map.setdefault(s.get("poi_id"), []).append((d_i, s))

                for poi_id, target_idx in moves.items():
                    if not isinstance(target_idx, int) or not (0 <= target_idx < len(days)):
                        continue
                    for src_idx, stop in idx_map.get(poi_id, []):
                        affected_days.add(src_idx)
                        affected_days.add(target_idx)
                        days[src_idx]["stops"] = [x for x in days[src_idx].get("stops", []) if x is not stop]

                        target_day = days[target_idx]
                        target_day.setdefault("stops", [])
                        target_pos = target_positions.get(poi_id)

                        if target_pos is not None and isinstance(target_pos, int):
                            stops = target_day["stops"]
                            is_depot = lambda s: s.get("role") in ("accommodation")
                            min_pos = 1 if stops and is_depot(stops[0]) else 0
                            max_pos = len(stops) - 1 if stops and is_depot(stops[-1]) else len(stops)
                            target_day["stops"].insert(max(min_pos, min(target_pos, max_pos)), stop)
                        else:
                            stops = target_day["stops"]
                            if stops and stops[-1].get("role") in ("accommodation"):
                                target_day["stops"].insert(len(stops) - 1, stop)
                            else:
                                target_day["stops"].append(stop)

            present = set(ordered)
            for d_idx, d in enumerate(days):
                prefix, core, suffix = _extract_core_stops(d.get("stops", []))
                by_id = {s["poi_id"]: s for s in core}
                new_core = [by_id[i] for i in ordered if i in by_id]
                for s in core:
                    if s["poi_id"] not in present:
                        new_core.append(s)
                d["stops"] = prefix + new_core + suffix
                _recompute_day_metrics(d)
                affected_days.add(d_idx)
        else:
            raise HTTPException(status_code=400, detail="Invalid scope")

        if recalculate_times:
            for d_idx in affected_days:
                if 0 <= d_idx < len(days):
                    _recalculate_day_times(days[d_idx], pacing)

        meta = data.setdefault("plan", {}).setdefault("meta", {})
        meta.setdefault("reorder", {}).update(
            {
                "overflow": False,
                "time_window_violation": False,
                "extended_hours": False,
                "respect_time_windows": bool(options.get("respect_time_windows", True)),
                "allow_overflow": bool(options.get("allow_overflow", True)),
            }
        )

        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to reorder itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/schedule-poi")
def schedule_poi(itin_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    """Schedule a POI (from days or ideas) to a specific day/time."""
    try:
        user_id = get_optional_user_id(authorization)
        payload = transform_frontend_to_canonical(payload)

        poi_id = payload.get("poi_id")
        day_index = payload.get("day_index")
        if not poi_id or day_index is None:
            raise HTTPException(status_code=400, detail="poiId and dayIndex are required")

        data = load_itinerary_with_auth(itin_id, user_id)
        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        day_index = int(day_index)
        if not (0 <= day_index < len(days)):
            raise HTTPException(status_code=400, detail="Invalid dayIndex")

        poi_stop, from_ideas = None, False
        for day in days:
            for stop in day.get("stops", []):
                if stop.get("poi_id") == poi_id:
                    poi_stop = stop
                    day["stops"] = [s for s in day["stops"] if s.get("poi_id") != poi_id]
                    break
            if poi_stop:
                break

        if not poi_stop:
            for idea in data.get("meta", {}).get("ideas", []):
                if idea.get("id") == poi_id:
                    from_ideas = True
                    poi_stop = {
                        "poi_id": idea.get("id"),
                        "name": idea.get("name"),
                        "role": idea.get("role") or "attraction",
                        "location": idea.get("location"),
                        "themes": idea.get("themes", []),
                        "images": idea.get("images"),
                        "coordinates": idea.get("coordinates"),
                    }
                    data["meta"]["ideas"] = [i for i in data["meta"]["ideas"] if i.get("id") != poi_id]
                    break

        if not poi_stop:
            raise HTTPException(status_code=404, detail="POI not found in itinerary or ideas")

        pacing = data.get("meta", {}).get("preferences", {}).get("pacing", "balanced")
        role = poi_stop.get("role", "attraction")
        duration = vrp_config.service_time_min.get(role, {}).get(pacing, 60)

        all_day = payload.get("all_day", False)
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        single_time = payload.get("single_time")

        if all_day:
            if from_ideas:
                target_day = days[day_index]
                last_depart = None
                for s in reversed(target_day.get("stops", [])):
                    if s.get("depart"):
                        last_depart = s["depart"]
                        break

                if last_depart:
                    try:
                        h, m = map(int, str(last_depart).split(":"))
                        start_min = h * 60 + m + 30
                        end_min = min(start_min + duration, 22 * 60)
                        start_min = min(start_min, end_min - 30)
                        poi_stop["arrival"] = f"{start_min // 60:02d}:{start_min % 60:02d}"
                        poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
                    except Exception:
                        poi_stop["arrival"] = "10:00"
                        poi_stop["depart"] = f"{10 + duration // 60:02d}:{duration % 60:02d}"
                else:
                    end_min = 10 * 60 + duration
                    poi_stop["arrival"] = "10:00"
                    poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
            else:
                poi_stop["arrival"] = poi_stop["depart"] = None
        elif start_time and end_time:
            poi_stop["arrival"] = start_time
            poi_stop["depart"] = end_time
        elif single_time:
            try:
                h, m = map(int, str(single_time).split(":"))
                start_min = h * 60 + m
                end_min = min(start_min + duration, 24 * 60)
                poi_stop["arrival"] = f"{start_min // 60:02d}:{start_min % 60:02d}"
                poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid singleTime format")
        elif (start_time and not end_time) or (end_time and not start_time):
            raise HTTPException(status_code=400, detail="Provide both start_time and end_time")
        else:
            raise HTTPException(status_code=400, detail="Provide allDay, both start/end times, or singleTime")

        target_day = days[day_index]
        target_day.setdefault("stops", [])
        target_position = payload.get("target_position")
        recalculate_times = payload.get("recalculate_times", True)

        if target_position is not None and isinstance(target_position, int):
            stops = target_day["stops"]
            is_depot = lambda s: s.get("role") in ("accommodation")
            min_pos = 1 if stops and is_depot(stops[0]) else 0
            max_pos = len(stops) - 1 if stops and is_depot(stops[-1]) else len(stops)
            target_day["stops"].insert(max(min_pos, min(target_position, max_pos)), poi_stop)
            if recalculate_times:
                _recalculate_day_times(target_day, pacing)
        else:
            target_day["stops"].append(poi_stop)
            _sort_day_stops_by_time(target_day)

        _recompute_day_metrics(target_day)
        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to schedule POI in itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}/poi/{poi_id}")
def delete_poi_from_itinerary(itin_id: str, poi_id: str, authorization: Optional[str] = Header(None)):
    """Remove a POI from the itinerary."""
    try:
        user_id = get_optional_user_id(authorization)
        data = load_itinerary_with_auth(itin_id, user_id)

        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        removed = False
        for day in data["plan"]["days"]:
            original_len = len(day.get("stops", []))
            day["stops"] = [s for s in day.get("stops", []) if s.get("poi_id") != poi_id]
            if len(day["stops"]) < original_len:
                removed = True

        if not removed:
            raise HTTPException(status_code=404, detail="POI not found in itinerary")

        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete POI from itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/poi/{poi_id}/lock")
def toggle_poi_lock(itin_id: str, poi_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    """Toggle lock status on a POI's scheduled time.
    
    When locked, the POI's start/end times will be preserved during optimization.
    """
    try:
        user_id = get_optional_user_id(authorization)
        data = load_itinerary_with_auth(itin_id, user_id)

        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        is_locked = payload.get("isLocked", False)
        found = False

        for day in data["plan"]["days"]:
            for stop in day.get("stops", []):
                if stop.get("poi_id") == poi_id:
                    stop["is_locked"] = is_locked
                    found = True
                    break
            if found:
                break

        if not found:
            raise HTTPException(status_code=404, detail="POI not found in itinerary")

        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to toggle lock on POI {poi_id} in itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}/idea/{idea_id}")
def delete_idea_from_itinerary(itin_id: str, idea_id: str, authorization: Optional[str] = Header(None)):
    """Remove an idea from the itinerary."""
    try:
        user_id = get_optional_user_id(authorization)
        data = load_itinerary_with_auth(itin_id, user_id)

        ideas = data.get("meta", {}).get("ideas", [])
        original_len = len(ideas)
        data.setdefault("meta", {})["ideas"] = [i for i in ideas if i.get("id") != idea_id]

        if len(data["meta"]["ideas"]) == original_len:
            raise HTTPException(status_code=404, detail="Idea not found in itinerary")

        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete idea from itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/update-meta")
def update_itinerary_meta(itin_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    """Update itinerary metadata and adjust plan days if needed."""
    try:
        user_id = get_optional_user_id(authorization)
        payload = transform_frontend_to_canonical(payload)
        data = load_itinerary_with_auth(itin_id, user_id)
        data.setdefault("meta", {})

        meta_updates = {}
        if "title" in payload:
            meta_updates["title"] = payload["title"]
        if "dates" in payload:
            meta_updates["dates"] = {**data["meta"].get("dates", {}), **payload["dates"]}
        if "travelers" in payload:
            meta_updates["travelers"] = {**data["meta"].get("travelers", {}), **payload["travelers"]}
        if "preferences" in payload:
            meta_updates["preferences"] = {**data["meta"].get("preferences", {}), **payload["preferences"]}
        if "flags" in payload:
            meta_updates["flags"] = {**data["meta"].get("flags", {}), **payload["flags"]}
        if "hotels" in payload:
            meta_updates["hotels"] = payload["hotels"]
        if "mandatory_pois" in payload:
            meta_updates["mandatory_pois"] = payload["mandatory_pois"]

        plan_changed = False
        data.setdefault("plan", {}).setdefault("days", [])
        days_list = data["plan"]["days"]
        current_days = len(days_list)

        new_days = compute_num_days(payload.get("dates", {})) if "dates" in payload else None
        if new_days and new_days != current_days:
            plan_changed = True
            if new_days > current_days:
                for _ in range(new_days - current_days):
                    days_list.append({"stops": []})
            else:
                data["meta"].setdefault("ideas", [])
                moved = 0
                from app.api.pois import get_poi_by_id

                for day in days_list[new_days:]:
                    for stop in day.get("stops", []):
                        poi_id = stop.get("poi_id")
                        if not poi_id:
                            continue
                        try:
                            res = get_poi_by_id(poi_id)
                            if res and res.get("data"):
                                poi = res["data"]
                                if poi.get("id") not in [i.get("id") for i in data["meta"]["ideas"]]:
                                    data["meta"]["ideas"].append(
                                        {
                                            "id": poi.get("id"),
                                            "name": poi.get("name"),
                                            "category": poi.get("category"),
                                            "rating": poi.get("rating"),
                                            "location": poi.get("location"),
                                            "images": poi.get("images", []),
                                        }
                                    )
                                    moved += 1
                        except Exception:
                            pass

                data["plan"]["days"] = days_list[:new_days]
                days_list = data["plan"]["days"]
                if moved:
                    data["meta"].setdefault("notices", []).append(f"{moved} POIs moved to ideas")
                meta_updates["ideas"] = data["meta"]["ideas"]

        if "dates" in payload and days_list:
            recompute_day_labels(days_list, meta_updates.get("dates") or data["meta"].get("dates"))
            plan_changed = True

        if meta_updates:
            success, error = update_itinerary_meta_for_user(itin_id, meta_updates, user_id)
            if not success:
                _handle_storage_error(error, "update")

        if plan_changed:
            success, error = update_itinerary_plan_for_user(itin_id, data["plan"], user_id)
            if not success:
                _handle_storage_error(error, "update")

        data["meta"].update(meta_updates)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to update itinerary metadata {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/add-poi")
def add_poi_to_itinerary(itin_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    """Add a POI to the itinerary's ideas list."""
    try:
        user_id = get_optional_user_id(authorization)
        payload = transform_frontend_to_canonical(payload)

        poi_id = payload.get("poi_id")
        if not poi_id:
            raise HTTPException(status_code=400, detail="poiId is required")

        data = load_itinerary_with_auth(itin_id, user_id)
        data.setdefault("meta", {}).setdefault("ideas", [])

        if poi_id in [i.get("id") for i in data["meta"]["ideas"]]:
            return transform_itinerary_response_to_frontend(data)

        for day in data.get("plan", {}).get("days", []):
            if any(s.get("poi_id") == poi_id for s in day.get("stops", [])):
                return transform_itinerary_response_to_frontend(data)

        from app.api.pois import get_poi_by_id

        res = get_poi_by_id(poi_id)
        if not res or "data" not in res:
            raise HTTPException(status_code=404, detail=f"POI {poi_id} not found")

        poi = res["data"]
        data["meta"]["ideas"].append(
            {
                "id": poi.get("id"),
                "name": poi.get("name"),
                "category": poi.get("category"),
                "categories": poi.get("categories"),
                "rating": poi.get("rating"),
                "reviews_count": poi.get("reviews_count"),
                "roles": poi.get("roles"),
                "role": (poi.get("roles") or ["attraction"])[0],
                "themes": poi.get("themes"),
                "location": poi.get("location"),
                "images": poi.get("images") or [],
                "coordinates": {"lat": poi.get("latitude"), "lng": poi.get("longitude")},
            }
        )

        save_itinerary(itin_id, data, user_id)
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add POI to itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))
