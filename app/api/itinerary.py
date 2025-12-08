import os
import json
import uuid
from fastapi import APIRouter, HTTPException
from app.services.transformers import (
    transform_frontend_payload,
    transform_response_to_frontend,
    transform_poi_to_frontend,
)
from app.services.maut import run_pipeline
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger
from app.services.vrp_model import vrp_config
from app.services.osrm import osrm_client
from app.utils.naming import transform_frontend_to_canonical

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["itinerary"])


def _normalize_destination_name(raw):
    """Normalize a location label to a city name the pipeline understands.
    Example: "Johor, Malaysia" -> "Johor".
    """
    if not raw:
        return None
    name = str(raw).strip()
    if "," in name:
        name = name.split(",")[0].strip()
    return name


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
    3. MAUT -> ACS-CVRPTW
    5. Persist to storage
    6. Return response

    Args:
        payload: Frontend CreateItineraryPayload

    Raises:
        HTTPException: 400 for invalid payload, 500 for processing errors
    """
    itin_id = str(uuid.uuid4())

    try:
        # 1. Ingress normalization to canonical snake_case
        try:
            payload = transform_frontend_to_canonical(payload)
            print(payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 2. Transform frontend → MAUT request
        maut_request = transform_frontend_payload(payload)

        # 3. Run MAUT pipeline (support multi-destination)
        destinations = (
            payload.get("destinations")
            if isinstance(payload.get("destinations"), list)
            else []
        )
        if destinations:
            all_places = []
            selected_themes_union: list[str] = []

            def _city_variants(name: str) -> list[str]:
                low = name.lower()
                if low == "johor":
                    return [name, "Johor Bahru"]
                return [name]

            for d in destinations:
                raw_city = d.get("city")
                city = _normalize_destination_name(raw_city)
                if not city:
                    continue
                out_i = None
                for cname in _city_variants(city):
                    req_i = dict(maut_request)
                    req_i["destination"] = cname
                    out_i = run_pipeline(req_i)
                    if out_i.get("places"):
                        break
                if not out_i:
                    continue
                # Tag fetched POIs with requested city for deterministic segmentation later
                tagged = []
                for p in out_i.get("places", []) or []:
                    try:
                        p["requested_city"] = city
                    except Exception:
                        pass
                    tagged.append(p)
                all_places.extend(tagged)
                th = out_i.get("meta", {}).get("selected_themes", [])
                for t in th:
                    if t not in selected_themes_union:
                        selected_themes_union.append(t)
            maut_output = {
                "status": "ok",
                "places": all_places,
                "total_distance": 0.0,
                "total_time": 0,
                "route_order": [],
                "meta": {
                    "selected_themes": selected_themes_union[:3]
                    if selected_themes_union
                    else maut_request.get("interest_themes", [])[:3],
                },
            }
        else:
            maut_output = run_pipeline(maut_request)

        # 3.5. Enrich MAUT output with dates and num_days for CVRPTW compatibility
        maut_output.setdefault("meta", {})
        maut_output["meta"]["dates"] = payload.get("dates", {})
        maut_output["meta"]["num_days"] = maut_request["num_days"]

        # 4. Extract hotel information from payload or MAUT output
        places = maut_output.get("places", [])

        # Check if hotels are provided in payload
        hotels_from_payload = payload.get("hotels", [])
        hotel = None

        # For multi-city requests, do not set a global hotel; let the pipeline select per-city hotels
        is_multi_city = bool(destinations)

        if hotels_from_payload and not is_multi_city:
            # Use first hotel from payload (single-city only)
            first_hotel = hotels_from_payload[0]
            hotel = {
                "id": first_hotel.get("poi_id"),
                "name": first_hotel.get("poi_name", "Hotel"),
                "lat": first_hotel.get("latitude"),
                "lon": first_hotel.get("longitude"),
            }
        elif not is_multi_city:
            # Fallback to MAUT-selected accommodation (single-city only)
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
        elif hotels_from_payload and is_multi_city:
            pass

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
                    "source": "user",
                }
                # Add to places if not already present
                if not any(p.get("id") == hotel_poi["id"] for p in places):
                    places.append(hotel_poi)
                    logger.info(f"Added hotel {hotel_poi['name']} to places")

        if mandatory_pois_from_payload:
            # Build canonical mandatory dict for solver adapter
            # Schema per POI: {
            #   "day": Optional[int],           # 1-based day index
            #   "window": Optional[[HH:MM, HH:MM]],  # time window
            #   "all_day": Optional[bool],      # blocks entire day
            #   "time_type": str                # 'specific' | 'all_day' | 'any_time'
            # }
            mandatory = {}
            dates_info = payload.get("dates", {})
            is_specific_dates = dates_info.get("type") == "specific"

            for poi in mandatory_pois_from_payload:
                poi_id = poi.get("poi_id")
                if not poi_id:
                    continue

                time_type = poi.get("time_type", "any_time")
                start_time = poi.get("start_time")
                end_time = poi.get("end_time")
                day = poi.get("day")
                date_str = poi.get("date")

                # Get poi_destination (normalize it like destination names)
                poi_dest_raw = poi.get("poi_destination")
                poi_destination = _normalize_destination_name(poi_dest_raw) if poi_dest_raw else None

                md_entry = {"time_type": time_type}
                if poi_destination:
                    md_entry["poi_destination"] = poi_destination

                # Handle day/date based on dates mode
                if is_specific_dates and date_str:
                    # Convert date to day index (1-based)
                    try:
                        from datetime import date as _date
                        trip_start_str = dates_info.get("start_date")
                        if trip_start_str:
                            trip_start = _date.fromisoformat(str(trip_start_str).split("T")[0])
                            poi_date = _date.fromisoformat(str(date_str).split("T")[0])
                            day_index = (poi_date - trip_start).days + 1  # 1-based
                            if day_index > 0:
                                md_entry["day"] = day_index
                    except Exception:
                        pass
                elif isinstance(day, int) and day > 0:
                    # Flexible mode: use day directly (already 1-based)
                    md_entry["day"] = day

                # Handle time_type modes
                if time_type == "all_day":
                    md_entry["all_day"] = True
                    # No window needed - solver will block entire day
                elif time_type == "specific" and start_time and end_time:
                    md_entry["window"] = [start_time, end_time]
                # else: any_time - no window constraint, solver uses role defaults

                # Even if neither day nor window is set, include entry to mark as mandatory
                mandatory[poi_id] = md_entry

                # Also add to places list for frontend display (idempotent)
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
                    "open_hours": poi.get("open_hours"),
                    "images": poi.get("images", []),
                }
                if not any(p.get("id") == mandatory_poi["id"] for p in places):
                    places.append(mandatory_poi)
                    logger.info(
                        f"Added mandatory POI {mandatory_poi['name']} to places"
                    )

            logger.info(
                f"Processing {len(mandatory)} mandatory POIs from payload (canonicalized)"
            )

        # Update maut_output with enriched places
        maut_output["places"] = places

        # Build user_hotels_by_city mapping (infer city by nearest POI with city)
        user_hotels_by_city = {}
        if hotels_from_payload:
            place_cities = [(p, p.get("area_name")) for p in places]
            for h in hotels_from_payload:
                hlat = h.get("latitude")
                hlon = h.get("longitude")
                if hlat is None or hlon is None:
                    continue
                best_city = None
                best_d = None
                for p, cname in place_cities:
                    if not cname:
                        continue
                    coords = p.get("coordinates") or {}
                    plat = coords.get("lat")
                    plon = coords.get("lng")
                    if plat is None or plon is None:
                        continue
                    d = (float(plat) - float(hlat)) ** 2 + (
                        float(plon) - float(hlon)
                    ) ** 2
                    if best_d is None or d < best_d:
                        best_d = d
                        best_city = cname
                if best_city:
                    user_hotels_by_city[best_city] = {
                        "id": h.get("poi_id"),
                        "name": h.get("poi_name", "Hotel"),
                        "lat": hlat,
                        "lon": hlon,
                        "source": "user",
                    }

        # Build days_per_city if multi-destination
        def _compute_total_days(dates_obj: dict, fallback_days: int) -> int:
            if not isinstance(dates_obj, dict):
                return fallback_days
            t = dates_obj.get("type")
            if (
                t == "specific"
                and dates_obj.get("start_date")
                and dates_obj.get("end_date")
            ):
                try:
                    from datetime import date as _date

                    s = _date.fromisoformat(str(dates_obj["start_date"]).split("T")[0])
                    e = _date.fromisoformat(str(dates_obj["end_date"]).split("T")[0])
                    return max(1, (e - s).days + 1)
                except Exception:
                    return fallback_days
            if t == "flexible" and dates_obj.get("days"):
                try:
                    d = int(dates_obj.get("days"))
                    return max(1, d)
                except Exception:
                    return fallback_days
            return fallback_days

        days_per_city = {}
        per_city_dates: dict[str, dict] = {}
        if destinations:
            total_days = _compute_total_days(
                payload.get("dates", {}), maut_request["num_days"]
            )
            provided: dict[str, int] = {}
            ordered_cities: list[str] = []

            def _days_from_dest_dates(dobj: dict) -> int | None:
                if not isinstance(dobj, dict):
                    return None
                if (
                    dobj.get("type") == "specific"
                    and dobj.get("start_date")
                    and dobj.get("end_date")
                ):
                    try:
                        from datetime import date as _date

                        s = _date.fromisoformat(str(dobj["start_date"]).split("T")[0])
                        e = _date.fromisoformat(str(dobj["end_date"]).split("T")[0])
                        return max(1, (e - s).days + 1)
                    except Exception:
                        return None
                return None

            for d in destinations:
                raw_city = d.get("city")
                city = _normalize_destination_name(raw_city)
                if not city:
                    continue
                ordered_cities.append(city)

                # Prefer explicit days, else derive from per-destination dates
                if d.get("days") is not None:
                    try:
                        provided[city] = max(1, int(d.get("days")))
                    except Exception:
                        provided[city] = 0
                elif isinstance(d.get("dates"), dict):
                    dd = _days_from_dest_dates(d.get("dates"))
                    if dd:
                        provided[city] = dd
                        # Preserve per-city specific dates window
                        per_city_dates[city] = {
                            "type": "specific",
                            "start_date": d.get("dates", {}).get("start_date"),
                            "end_date": d.get("dates", {}).get("end_date"),
                        }

            if provided:
                s = sum(max(0, v) for v in provided.values())
                if s > 0 and s != total_days:
                    ratio = total_days / s
                    base = {
                        k: max(0, int(round(v * ratio))) for k, v in provided.items()
                    }
                    diff = total_days - sum(base.values())
                    for k in ordered_cities:
                        if k in base and diff != 0:
                            adjust = 1 if diff > 0 else -1
                            base[k] += adjust
                            diff -= adjust
                            if diff == 0:
                                break
                    days_per_city = base
                else:
                    days_per_city = {k: max(1, int(v)) for k, v in provided.items()}
            else:
                k = len(ordered_cities)
                if k > 0:
                    q, r = divmod(total_days, k)
                    for i, c in enumerate(ordered_cities):
                        days_per_city[c] = q + (1 if i < r else 0)

        user_input = (
            {"user_hotels_by_city": user_hotels_by_city}
            if user_hotels_by_city
            else None
        )
        if days_per_city:
            user_input = user_input or {}
            user_input["days_per_city"] = days_per_city
            user_input["city_order"] = ordered_cities
        if per_city_dates:
            user_input = user_input or {}
            user_input["per_city_dates"] = per_city_dates

        # 6. Run full pipeline
        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            hotel=hotel,
            pacing=maut_request.get("pacing", "balanced"),
            mandatory=mandatory,
            time_limit_sec=20,
            solver="acs",
            user_input=user_input,
        )

        # 6. Transform pipeline output → frontend plan
        if pipeline_output.get("status") == "success":
            # Multi-city integrity check: ensure distinct destinations in days match request intent
            try:
                # Build expected cities from request (normalized)
                expected: list[str] = []
                for d in destinations or []:
                    raw_city = d.get("city")
                    c = _normalize_destination_name(raw_city)
                    if c:
                        expected.append(c)
                # Collect actual destinations from solver output days
                days_out = pipeline_output.get("days", []) or []
                actual_set = {
                    str(day.get("destination") or day.get("area_name") or "").strip()
                    for day in days_out
                    if day.get("destination") or day.get("area_name")
                }
                actual = sorted(x for x in actual_set if x)

                if expected and len(expected) > 1:
                    if len(actual) < len(expected):
                        logger.error(
                            "multi_city_integrity_failed: expected=%s actual=%s request_id=%s",
                            expected,
                            actual,
                            pipeline_output.get("meta", {}).get("request_id"),
                        )
                        raise HTTPException(
                            status_code=500,
                            detail={
                                "status": "error",
                                "error": "multi_city_collapsed",
                                "message": "Multi-city request collapsed to fewer destinations in solver output",
                                "expected_cities": expected,
                                "actual_cities": actual,
                            },
                        )
            except HTTPException:
                raise
            except Exception:
                # Do not block on diagnostics failure
                pass

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
                "destination": maut_request.get("destination"),  # legacy
                "destinations": destinations or [],
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
    Reorder itinerary stops with support for single-day and entire-trip scopes.

    Payload options:
    - scope: "single_day" | "entire_trip" (default: single_day)
    - day_index: required if scope == single_day
    - ordered_poi_ids: list[str] desired order
    - moves: Optional[dict[str,int]] mapping poi_id -> target day_index (for cross-day moves)
    - options: { respect_time_windows?: bool (default True), allow_overflow?: bool (default True), idempotency_key?: str }

    Behavior:
    - No-drop invariant: Do not drop any POIs.
    - Recompute per-day distance metrics.
    - Annotate basic flags (placeholders): overflow, time_window_violation, extended_hours.
    """
    try:
        scope = payload.get("scope") or "single_day"
        ordered = payload.get("ordered_poi_ids") or payload.get("poi_ids") or []
        options = payload.get("options") or {}
        moves = payload.get("moves") or {}

        if not isinstance(ordered, list):
            raise HTTPException(
                status_code=400, detail="ordered_poi_ids must be a list"
            )

        data = load_itinerary(itin_id)
        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]

        if scope == "single_day":
            day_index = payload.get("day_index")
            if day_index is None or not (0 <= int(day_index) < len(days)):
                raise HTTPException(
                    status_code=400, detail="day_index is required and must be valid"
                )

            day = days[int(day_index)]
            stops = day.get("stops", [])
            # Build lookup and preserve depot/hotel positions
            first = (
                stops[0]
                if stops and stops[0].get("role") in ("depot", "hotel")
                else None
            )
            last = (
                stops[-1]
                if stops and stops[-1].get("role") in ("depot", "hotel")
                else None
            )
            core = (
                stops[1:-1]
                if (first and last and first is not last)
                else (stops[1:] if first else (stops[:-1] if last else stops))
            )
            prefix = [first] if first else []
            suffix = [last] if last and last is not first else []

            by_id = {s["poi_id"]: s for s in core}
            new_core = [by_id[i] for i in ordered if i in by_id]
            # Append any not specified to maintain no-drop
            for s in core:
                if s["poi_id"] not in ordered:
                    new_core.append(s)

            new_stops = prefix + new_core + suffix
            day["stops"] = new_stops

            # Recompute metrics and sort by arrival for consistency
            _recompute_day_metrics(day)
            _sort_day_stops_by_time(day)

        elif scope == "entire_trip":
            # Apply cross-day moves if provided
            if isinstance(moves, dict) and moves:
                # Build index of all stops by poi_id
                idx_map = {}
                for d_i, d in enumerate(days):
                    for s in d.get("stops", []):
                        idx_map.setdefault(s.get("poi_id"), []).append((d_i, s))
                # Move each specified poi_id to target day
                for poi_id, target_day in moves.items():
                    if not isinstance(target_day, int) or not (
                        0 <= target_day < len(days)
                    ):
                        continue
                    locs = idx_map.get(poi_id) or []
                    for src_day, stop in locs:
                        # Remove from source
                        src_list = days[src_day].get("stops", [])
                        days[src_day]["stops"] = [x for x in src_list if x is not stop]
                        # Append to target
                        days[target_day].setdefault("stops", [])
                        days[target_day]["stops"].append(stop)

            # Reorder within each day using the subsequence present
            present = set(ordered)
            for d in days:
                stops = d.get("stops", [])
                first = (
                    stops[0]
                    if stops and stops[0].get("role") in ("depot", "hotel")
                    else None
                )
                last = (
                    stops[-1]
                    if stops and stops[-1].get("role") in ("depot", "hotel")
                    else None
                )
                core = (
                    stops[1:-1]
                    if (first and last and first is not last)
                    else (stops[1:] if first else (stops[:-1] if last else stops))
                )
                prefix = [first] if first else []
                suffix = [last] if last and last is not first else []

                by_id = {s["poi_id"]: s for s in core}
                new_core = [by_id[i] for i in ordered if i in by_id]
                for s in core:
                    if s["poi_id"] not in present:
                        new_core.append(s)
                d["stops"] = prefix + new_core + suffix
                _recompute_day_metrics(d)
                _sort_day_stops_by_time(d)
        else:
            raise HTTPException(status_code=400, detail="Invalid scope")

        # Annotate basic reorder flags in meta
        meta = data.setdefault("plan", {}).setdefault("meta", {})
        meta.setdefault("reorder", {})
        meta["reorder"].update(
            {
                "overflow": False,
                "time_window_violation": False,
                "extended_hours": False,
                "respect_time_windows": bool(options.get("respect_time_windows", True)),
                "allow_overflow": bool(options.get("allow_overflow", True)),
            }
        )

        save_itinerary(itin_id, data)
        # logger.info(f"Reordered itinerary {itin_id} with scope={scope}")
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

    # Depot at start and/or end
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


def _recompute_day_metrics(day: dict) -> None:
    """Recompute a day's total distance using OSRM distance for sequential stops."""
    stops = day.get("stops", [])
    total = 0.0
    if len(stops) >= 2:
        for i in range(len(stops) - 1):
            lat1 = stops[i].get("coordinates", {}).get("lat") or stops[i].get(
                "latitude"
            )
            lon1 = stops[i].get("coordinates", {}).get("lng") or stops[i].get(
                "longitude"
            )
            lat2 = stops[i + 1].get("coordinates", {}).get("lat") or stops[i + 1].get(
                "latitude"
            )
            lon2 = stops[i + 1].get("coordinates", {}).get("lng") or stops[i + 1].get(
                "longitude"
            )
            if None not in (lat1, lon1, lat2, lon2):
                try:
                    total += osrm_client.distance(lat1, lon1, lat2, lon2)
                except Exception:
                    continue
    day["total_distance"] = round(total, 2)


@router.post("/itinerary/{itin_id}/schedule-poi")
def schedule_poi(itin_id: str, payload: dict):
    """
    Update POI schedule (time or move to different day) with strict input modes.

    Allowed modes:
    - all_day: true
    - start_time and end_time both provided (HH:MM)
    - single_time: "HH:MM" (infer end_time from role/pacing defaults)

    Reject payloads with only one of start_time/end_time.
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
        day_index = int(day_index)
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

        # Mode validation
        all_day = bool(payload.get("all_day", False))
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        single_time = payload.get("single_time")

        if all_day:
            poi_stop["arrival"] = None
            poi_stop["start_service"] = None
            poi_stop["depart"] = None
        else:
            if (start_time and not end_time) or (end_time and not start_time):
                raise HTTPException(
                    status_code=400,
                    detail="Provide both start_time and end_time, or use single_time/all_day",
                )

            if start_time and end_time:
                poi_stop["arrival"] = start_time
                poi_stop["start_service"] = start_time
                poi_stop["depart"] = end_time
            elif single_time:
                # Infer duration from role and pacing
                pacing = data.get("plan", {}).get("meta", {}).get("pacing", "balanced")
                role = poi_stop.get("role", "attraction")
                try:
                    duration_min = int(vrp_config.service_time_min.get(role, {}).get(pacing, 60))
                except Exception:
                    duration_min = 60
                # Compute end time HH:MM
                try:
                    h, m = map(int, str(single_time).split(":"))
                    start_min = h * 60 + m
                    end_min = start_min + max(0, duration_min)
                    end_min = min(end_min, 24 * 60)
                    poi_stop["arrival"] = f"{start_min // 60:02d}:{start_min % 60:02d}"
                    poi_stop["start_service"] = poi_stop["arrival"]
                    poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid single_time format; expected HH:MM",
                    )
            else:
                # No valid mode
                raise HTTPException(
                    status_code=400,
                    detail="Provide all_day, both start/end times, or single_time",
                )

        # Add to target day and sort
        target_day = days[day_index]
        target_day.setdefault("stops", [])
        target_day["stops"].append(poi_stop)

        _sort_day_stops_by_time(target_day)
        _recompute_day_metrics(target_day)

        save_itinerary(itin_id, data)
        # logger.info(f"Scheduled POI {poi_id} in itinerary {itin_id}")
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
        # logger.info(f"Deleted POI {poi_id} from itinerary {itin_id}")

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
            if t == "specific" and dates.get("start_date") and dates.get("end_date"):
                try:
                    from datetime import date as _date

                    s = _date.fromisoformat(str(dates["start_date"]).split("T")[0])
                    e = _date.fromisoformat(str(dates["end_date"]).split("T")[0])
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
