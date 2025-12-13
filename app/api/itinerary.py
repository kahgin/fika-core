import os
import json
import uuid
from typing import Optional
from fastapi import APIRouter, HTTPException, Header
from app.services.transformers import (
    transform_frontend_payload,
    transform_response_to_frontend,
    transform_poi_to_frontend,
    transform_itinerary_response_to_frontend,
)
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.utils.logger import get_logger
from app.services.vrp_model import vrp_config
from app.services.osrm import osrm_client
from app.utils.naming import transform_frontend_to_canonical
from app.utils.date_utils import recompute_day_labels, time_to_minutes

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


# Auth helper - import from auth module
def get_optional_user_id(authorization: Optional[str]) -> Optional[str]:
    """Get user ID from authorization header if valid, None otherwise."""
    if not authorization:
        return None
    try:
        from app.api.auth import get_user_from_token

        token = (
            authorization.replace("Bearer ", "")
            if authorization.startswith("Bearer ")
            else authorization
        )
        user = get_user_from_token(token)
        return user["id"] if user else None
    except Exception:
        return None


# Storage Helpers

# Import database storage functions
try:
    from app.db.itinerary_storage import (
        save_itinerary_to_db,
        load_itinerary_from_db,
        soft_delete_itinerary_for_user,
        list_itineraries_from_db,
        load_itinerary_for_user,
        update_itinerary_plan_for_user,
    )

    DB_STORAGE_AVAILABLE = True
except ImportError:
    DB_STORAGE_AVAILABLE = False
    logger.warning("Database storage not available, using file storage only")


def get_storage_dir() -> str:
    """Get absolute path to itineraries storage directory."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "storage",
        "itineraries",
    )


def save_itinerary(itin_id: str, data: dict, user_id: Optional[str] = None) -> None:
    """Persist itinerary to storage (database primary, with user_id if provided)."""
    # Add user_id to meta if provided
    if user_id:
        data.setdefault("meta", {})["user_id"] = user_id

    # Always try to save to database (works for both logged-in and anonymous users)
    if DB_STORAGE_AVAILABLE:
        try:
            if save_itinerary_to_db(itin_id, data):
                # logger.info(f"Saved itinerary {itin_id} to database" + (f" for user {user_id}" if user_id else " (anonymous)"))
                return
        except Exception as e:
            logger.warning(
                f"Database save failed for {itin_id}: {e}"
            )

    # File storage for anonymous users or as fallback
    # storage_dir = get_storage_dir()
    # os.makedirs(storage_dir, exist_ok=True)

    # storage_path = os.path.join(storage_dir, f"{itin_id}.json")
    # with open(storage_path, "w", encoding="utf-8") as f:
    #     json.dump(data, f, ensure_ascii=False, indent=2)
    # logger.info(f"Saved itinerary {itin_id} to file storage (anonymous)")


def load_itinerary(itin_id: str, user_id: Optional[str] = None) -> dict:
    """Load itinerary from storage (database primary, file fallback)."""
    # Try database storage first
    if DB_STORAGE_AVAILABLE:
        try:
            data = load_itinerary_from_db(itin_id)
            return data
        except Exception as e:
            logger.warning(f"Database load failed for {itin_id}, trying file: {e}")

    # Fallback to file storage
    storage_path = os.path.join(get_storage_dir(), f"{itin_id}.json")

    if not os.path.exists(storage_path):
        raise FileNotFoundError(f"Itinerary {itin_id} not found")

    with open(storage_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Migrate file storage to database if available
    if DB_STORAGE_AVAILABLE:
        try:
            save_itinerary_to_db(itin_id, data)
            # logger.info(f"Migrated itinerary {itin_id} from file to database")
        except Exception as e:
            logger.warning(f"Failed to migrate {itin_id} to database: {e}")

    return data


def load_itinerary_with_auth(itin_id: str, user_id: Optional[str]) -> dict:
    """
    Load itinerary with ownership verification.
    
    For logged-in users: Uses RPC to verify ownership and return data.
    For anonymous users: Falls back to regular load.
    
    Raises:
        HTTPException: 403 if not owner, 404 if not found
    """
    if DB_STORAGE_AVAILABLE and user_id:
        data, error = load_itinerary_for_user(itin_id, user_id)
        if error == "forbidden":
            raise HTTPException(
                status_code=403,
                detail="You don't have permission to access this itinerary",
            )
        if error == "not_found":
            raise HTTPException(status_code=404, detail="Itinerary not found")
        if data:
            return data
    
    # Fallback to regular load for anonymous users
    return load_itinerary(itin_id, user_id)


def update_itinerary_plan(itin_id: str, plan: dict, user_id: Optional[str]) -> bool:
    """
    Update itinerary plan with ownership verification (for logged-in users).
    
    Raises:
        HTTPException: 403 if not owner, 404 if not found
    """
    if DB_STORAGE_AVAILABLE and user_id:
        success, error = update_itinerary_plan_for_user(itin_id, plan, user_id)
        if error == "forbidden":
            raise HTTPException(
                status_code=403,
                detail="You don't have permission to modify this itinerary",
            )
        if error == "not_found":
            raise HTTPException(status_code=404, detail="Itinerary not found")
        return success
    return False


# API Endpoints


@router.post("/itinerary/create")
def create_itinerary(payload: dict, authorization: Optional[str] = Header(None)):
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
        authorization: Optional Bearer token for authenticated users

    Raises:
        HTTPException: 400 for invalid payload, 500 for processing errors
    """
    itin_id = str(uuid.uuid4())
    user_id = get_optional_user_id(authorization)

    try:
        # 1. Ingress normalization to canonical snake_case
        try:
            payload = transform_frontend_to_canonical(payload)
            # print(payload)
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
                    out_i = run_maut(req_i)
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
            maut_output = run_maut(maut_request)

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
                p for p in places if "accommodation" in p.get("roles", [])
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
                # Normalize destination to area_name for proper city segmentation
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
                    "roles": [hotel_data.get("role", "accommodation")],
                    "themes": hotel_data.get("themes", []),
                    "open_hours": hotel_data.get("open_hours"),
                    "images": hotel_data.get("images", []),
                    "source": "user",
                }
                # Set area_name for city segmentation (critical for multi-city)
                if hotel_area_name:
                    hotel_poi["area_name"] = hotel_area_name
                # Add to places if not already present
                if not any(p.get("id") == hotel_poi["id"] for p in places):
                    places.append(hotel_poi)
                    logger.info(f"Added hotel {hotel_poi['name']} to places")

        if mandatory_pois_from_payload:
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

                # Get poi_destination
                poi_dest_raw = poi.get("poi_destination")
                poi_destination = (
                    _normalize_destination_name(poi_dest_raw) if poi_dest_raw else None
                )

                md_entry = {"time_type": time_type}
                if poi_destination:
                    md_entry["poi_destination"] = poi_destination

                # Store additional info for tracking/ideas
                md_entry["poi_name"] = poi.get("poi_name", "Unknown POI")
                md_entry["role"] = poi.get("role", "attraction")
                md_entry["themes"] = poi.get("themes", [])
                md_entry["images"] = poi.get("images", [])

                # Handle day/date based on dates mode
                if is_specific_dates and date_str:
                    # Convert date to day index (1-based)
                    try:
                        from datetime import date as _date

                        trip_start_str = dates_info.get("start_date")
                        if trip_start_str:
                            trip_start = _date.fromisoformat(
                                str(trip_start_str).split("T")[0]
                            )
                            poi_date = _date.fromisoformat(str(date_str).split("T")[0])
                            day_index = (poi_date - trip_start).days + 1  # 1-based
                            if day_index > 0:
                                md_entry["day"] = day_index
                    except Exception:
                        pass
                elif isinstance(day, int) and day > 0:
                    # Flexible mode: use day directly (already 1-based)
                    md_entry["day"] = day

                # Handle time_type modes (support both camelCase and snake_case)
                if time_type in ("all_day", "allDay"):
                    md_entry["all_day"] = True
                    md_entry["time_type"] = "all_day"  # Normalize to snake_case
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
                    "roles": [poi.get("role", "attraction")],
                    "area_name": poi.get("poi_destination"),
                    "themes": poi.get("themes", []),
                    "open_hours": poi.get("open_hours"),
                    "images": poi.get("images", []),
                }
                if not any(p.get("id") == mandatory_poi["id"] for p in places):
                    places.append(mandatory_poi)
                    # logger.info(f"Added mandatory POI {mandatory_poi['name']} to places")

            # logger.info(f"Processing {len(mandatory)} mandatory POIs from payload (canonicalized)")

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
        # Extract mandatory_ideas from pipeline output to populate ideas
        pipeline_meta = pipeline_output.get("meta", {}) if pipeline_output else {}
        mandatory_ideas = pipeline_meta.get("mandatory_ideas", [])

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
                "ideas": mandatory_ideas,  # Populate with unfulfilled mandatory POIs
            },
            "plan": plan,
        }

        # 6. Persist to storage
        save_itinerary(itin_id, result, user_id)

        # Return camelCase response to frontend
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


@router.get("/itinerary/{itin_id}")
def get_itinerary(itin_id: str, authorization: Optional[str] = Header(None)):
    """
    Retrieve an existing itinerary by ID.

    Authorization check:
    - If itinerary has no owner (null user_id), anyone can access
    - If itinerary has an owner, only that user can access

    Args:
        itin_id: Itinerary identifier
        authorization: Optional Bearer token

    Returns:
        Full itinerary data in camelCase format

    Raises:
        HTTPException: 403 if not owner, 404 if not found, 500 for errors
    """
    try:
        user_id = get_optional_user_id(authorization)

        # Try database first with ownership check
        if DB_STORAGE_AVAILABLE:
            data, error = load_itinerary_for_user(itin_id, user_id)
            if error == "forbidden":
                raise HTTPException(
                    status_code=403,
                    detail="You don't have permission to access this itinerary",
                )
            if data:
                return transform_itinerary_response_to_frontend(data)
            # If not found in DB, fall through to file storage

        # Fallback to file storage (legacy - no ownership check for file storage)
        data = load_itinerary(itin_id)
        return transform_itinerary_response_to_frontend(data)
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Itinerary not found")
    except Exception as e:
        logger.exception(f"Failed to load itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/itineraries")
def list_itineraries(authorization: Optional[str] = Header(None)):
    """
    List stored itineraries for the authenticated user.

    For authenticated users: Returns their itineraries from database.
    For anonymous users: Returns empty list (they should use local storage).

    Returns:
        List of itinerary metadata in camelCase format
    """
    try:
        user_id = get_optional_user_id(authorization)

        # For authenticated users, get from database
        if DB_STORAGE_AVAILABLE and user_id:
            try:
                db_itineraries, total_count = list_itineraries_from_db(user_id)
                # Transform each itinerary to proper format
                result = []
                for itin_summary in db_itineraries:
                    # Load full itinerary for proper transformation
                    full_itin = load_itinerary_from_db(itin_summary.get("id"))
                    if full_itin:
                        result.append(
                            transform_itinerary_response_to_frontend(full_itin)
                        )
                return result
            except Exception as e:
                logger.warning(f"Database list failed for user {user_id}: {e}")

        # For anonymous users, return empty (frontend uses local storage)
        if not user_id:
            return []

        # Fallback: list from file storage (legacy)
        storage_dir = get_storage_dir()
        if not os.path.exists(storage_dir):
            return []

        itineraries = []
        for filename in os.listdir(storage_dir):
            if filename.endswith(".json"):
                try:
                    itin_id = filename.replace(".json", "")
                    data = load_itinerary(itin_id)
                    itineraries.append(transform_itinerary_response_to_frontend(data))
                except Exception as e:
                    logger.warning(f"Failed to load itinerary {filename}: {e}")
                    continue

        return itineraries
    except Exception as e:
        logger.exception("Failed to list itineraries")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}")
def delete_itinerary(itin_id: str, authorization: Optional[str] = Header(None)):
    """
    Delete an itinerary by ID (soft delete - sets status to 'deleted').

    Authorization check:
    - If itinerary has no owner (null user_id), anyone can delete
    - If itinerary has an owner, only that user can delete

    Args:
        itin_id: Itinerary identifier
        authorization: Optional Bearer token

    Returns:
        {"status": "deleted", "itinId": str}

    Raises:
        HTTPException: 403 if not owner, 404 if not found, 500 for errors
    """
    try:
        user_id = get_optional_user_id(authorization)

        # Try database first with ownership check and soft delete
        if DB_STORAGE_AVAILABLE:
            success, error = soft_delete_itinerary_for_user(itin_id, user_id)
            if error == "forbidden":
                raise HTTPException(
                    status_code=403,
                    detail="You don't have permission to delete this itinerary",
                )
            if error == "not_found":
                # Fall through to file storage check
                pass
            elif success:
                return {"status": "deleted", "itinId": itin_id}

        # Fallback to file storage (legacy - hard delete, no ownership check)
        storage_path = os.path.join(get_storage_dir(), f"{itin_id}.json")

        if not os.path.exists(storage_path):
            raise HTTPException(status_code=404, detail="Itinerary not found")

        os.remove(storage_path)

        return {"status": "deleted", "itinId": itin_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/reorder")
def reorder_itinerary_stops(
    itin_id: str,
    payload: dict,
    authorization: Optional[str] = Header(None),
):
    """
    Reorder itinerary stops with support for single-day and entire-trip scopes.

    Payload options (camelCase from frontend, converted to snake_case):
    - scope: "single_day" | "entire_trip" (default: single_day)
    - dayIndex: required if scope == single_day
    - orderedPoiIds: list[str] desired order
    - moves: Optional[dict[str,int]] mapping poiId -> target dayIndex (for cross-day moves)
    - targetPositions: Optional[dict[str,int]] mapping poiId -> target position index within day
    - recalculateTimes: bool (default True) - whether to recalculate arrival/depart times
    - options: { respectTimeWindows?: bool (default True), allowOverflow?: bool (default True), idempotencyKey?: str }

    Behavior:
    - No-drop invariant: Do not drop any POIs.
    - Recompute per-day distance metrics.
    - Recalculate times based on travel durations (if enabled).
    - Annotate basic flags (placeholders): overflow, time_window_violation, extended_hours.
    """
    try:
        user_id = get_optional_user_id(authorization)
        # Convert payload from camelCase to snake_case
        payload = transform_frontend_to_canonical(payload)

        scope = payload.get("scope") or "single_day"
        ordered = payload.get("ordered_poi_ids") or payload.get("poi_ids") or []
        options = payload.get("options") or {}
        moves = payload.get("moves") or {}
        target_positions = payload.get("target_positions") or {}
        recalculate_times = payload.get("recalculate_times", True)

        if not isinstance(ordered, list):
            raise HTTPException(
                status_code=400, detail="ordered_poi_ids must be a list"
            )

        # Load with ownership verification for logged-in users
        data = load_itinerary_with_auth(itin_id, user_id)
        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        pacing = data.get("meta", {}).get("preferences", {}).get("pacing", "balanced")
        
        # Track which days were affected for time recalculation
        affected_days = set()

        if scope == "single_day":
            day_index = payload.get("day_index")
            if day_index is None or not (0 <= int(day_index) < len(days)):
                raise HTTPException(
                    status_code=400, detail="day_index is required and must be valid"
                )

            day_index = int(day_index)
            day = days[day_index]
            stops = day.get("stops", [])
            # Build lookup and preserve depot/hotel/accommodation positions
            first = (
                stops[0]
                if stops and stops[0].get("role") in ("depot", "hotel", "accommodation")
                else None
            )
            last = (
                stops[-1]
                if stops
                and stops[-1].get("role") in ("depot", "hotel", "accommodation")
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
            affected_days.add(day_index)

            # Recompute metrics but DON'T sort - respect user's manual ordering
            _recompute_day_metrics(day)

        elif scope == "entire_trip":
            # Apply cross-day moves if provided
            if isinstance(moves, dict) and moves:
                # Build index of all stops by poi_id
                idx_map = {}
                for d_i, d in enumerate(days):
                    for s in d.get("stops", []):
                        idx_map.setdefault(s.get("poi_id"), []).append((d_i, s))
                # Move each specified poi_id to target day
                for poi_id, target_day_idx in moves.items():
                    if not isinstance(target_day_idx, int) or not (
                        0 <= target_day_idx < len(days)
                    ):
                        continue
                    locs = idx_map.get(poi_id) or []
                    for src_day_idx, stop in locs:
                        # Track affected days
                        affected_days.add(src_day_idx)
                        affected_days.add(target_day_idx)
                        
                        # Remove from source
                        src_list = days[src_day_idx].get("stops", [])
                        days[src_day_idx]["stops"] = [x for x in src_list if x is not stop]
                        
                        # Get target position if specified
                        target_pos = target_positions.get(poi_id)
                        target_day = days[target_day_idx]
                        target_day.setdefault("stops", [])
                        
                        if target_pos is not None and isinstance(target_pos, int):
                            # Insert at specific position (respecting depot/hotel boundaries)
                            stops = target_day["stops"]
                            # Determine valid insertion range (after first depot, before last depot)
                            min_pos = 1 if stops and stops[0].get("role") in ("depot", "hotel", "accommodation") else 0
                            max_pos = len(stops) - 1 if stops and stops[-1].get("role") in ("depot", "hotel", "accommodation") else len(stops)
                            
                            # Clamp position to valid range
                            insert_pos = max(min_pos, min(target_pos, max_pos))
                            target_day["stops"].insert(insert_pos, stop)
                        else:
                            # Append to end (before final depot if present)
                            stops = target_day["stops"]
                            if stops and stops[-1].get("role") in ("depot", "hotel", "accommodation"):
                                target_day["stops"].insert(len(stops) - 1, stop)
                            else:
                                target_day["stops"].append(stop)

            # Reorder within each day using the subsequence present
            present = set(ordered)
            for d_idx, d in enumerate(days):
                stops = d.get("stops", [])
                first = (
                    stops[0]
                    if stops
                    and stops[0].get("role") in ("depot", "hotel", "accommodation")
                    else None
                )
                last = (
                    stops[-1]
                    if stops
                    and stops[-1].get("role") in ("depot", "hotel", "accommodation")
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
                affected_days.add(d_idx)
                # DON'T sort - respect user's manual ordering
        else:
            raise HTTPException(status_code=400, detail="Invalid scope")

        # Recalculate times for affected days if enabled
        if recalculate_times:
            for d_idx in affected_days:
                if 0 <= d_idx < len(days):
                    _recalculate_day_times(days[d_idx], pacing)

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

        # Log the new order before saving
        # logger.info(f"Reorder {itin_id}: saving with days order = {[[s.get('poi_id') for s in d.get('stops', [])] for d in days]}")
        save_itinerary(itin_id, data, user_id)
        # logger.info(f"Reordered itinerary {itin_id} with scope={scope}, user_id={user_id}")
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to reorder itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


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
        return role in ("depot", "hotel", "accommodation")

    def sort_key(stop: dict):
        arrival = stop.get("arrival")
        if arrival:
            # Use large default (24*60+1=1441) so invalid times sort last
            return (0, time_to_minutes(arrival, default=24 * 60 + 1))
        # all-day / no time
        return (1, time_to_minutes("23:59", default=24 * 60 + 1))

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


def _recalculate_day_times(day: dict, pacing: str = "balanced") -> None:
    """
    Recalculate arrival/start_service/depart times for all stops in sequence.
    
    Uses OSRM for travel time between consecutive stops and respects
    each POI's service time based on role and pacing.
    
    Performance: ~5-20ms per OSRM call with local OSRM server.
    A day with 6 stops = ~5 calls = ~25-100ms total.
    """
    stops = day.get("stops", [])
    if not stops:
        return
    
    # Get service time config
    service_times = vrp_config.service_time_min
    
    def get_service_duration(stop: dict) -> int:
        """Get service duration in minutes for a stop."""
        role = stop.get("role", "attraction")
        # Check for custom duration first
        custom_duration = stop.get("duration_min") or stop.get("service_time")
        if custom_duration:
            try:
                return int(custom_duration)
            except (ValueError, TypeError):
                pass
        # Use config-based duration
        return service_times.get(role, {}).get(pacing, 60)
    
    def get_coords(stop: dict) -> tuple:
        """Extract lat/lon from stop."""
        lat = stop.get("coordinates", {}).get("lat") or stop.get("latitude")
        lon = stop.get("coordinates", {}).get("lng") or stop.get("longitude")
        return lat, lon
    
    def minutes_to_time(minutes: int) -> str:
        """Convert minutes from midnight to HH:MM."""
        minutes = max(0, min(minutes, 24 * 60 - 1))  # Clamp to valid range
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    
    # Determine start time from first stop or use default based on pacing
    first_stop = stops[0]
    start_min = None
    
    # Try to get existing arrival time from first stop
    if first_stop.get("arrival"):
        try:
            h, m = map(int, str(first_stop["arrival"]).split(":"))
            start_min = h * 60 + m
        except (ValueError, TypeError):
            pass
    
    # Fallback to pacing-based start time
    if start_min is None:
        start_min = vrp_config.pace_day_start_min.get(pacing, 9 * 60)
    
    current_time = start_min
    
    for i, stop in enumerate(stops):
        role = stop.get("role", "attraction")
        
        # Skip depot/hotel at start - they don't consume time
        if i == 0 and role in ("depot", "hotel", "accommodation"):
            # Set departure time for depot
            stop["arrival"] = minutes_to_time(current_time)
            stop["start_service"] = stop["arrival"]
            stop["depart"] = stop["arrival"]  # Immediate departure
            continue
        
        # Calculate travel time from previous stop
        if i > 0:
            prev_stop = stops[i - 1]
            lat1, lon1 = get_coords(prev_stop)
            lat2, lon2 = get_coords(stop)
            
            if None not in (lat1, lon1, lat2, lon2):
                try:
                    # Get travel time in seconds, convert to minutes
                    travel_seconds = osrm_client.route(lat1, lon1, lat2, lon2)
                    travel_minutes = int(travel_seconds / 60) + 1  # Round up
                    current_time += travel_minutes
                except Exception:
                    # Fallback: assume 15 min travel time
                    current_time += 15
            else:
                # No coordinates, assume 15 min travel time
                current_time += 15
        
        # Skip time consumption for ending depot/hotel
        if i == len(stops) - 1 and role in ("depot", "hotel", "accommodation"):
            stop["arrival"] = minutes_to_time(current_time)
            stop["start_service"] = stop["arrival"]
            stop["depart"] = None  # End of day
            continue
        
        # Set arrival and service times
        arrival = current_time
        service_duration = get_service_duration(stop)
        depart = current_time + service_duration
        
        stop["arrival"] = minutes_to_time(arrival)
        stop["start_service"] = minutes_to_time(arrival)
        stop["depart"] = minutes_to_time(depart)
        
        # Move current time forward
        current_time = depart


@router.post("/itinerary/{itin_id}/schedule-poi")
def schedule_poi(
    itin_id: str, payload: dict, authorization: Optional[str] = Header(None)
):
    """
    Update POI schedule (time or move to different day) with strict input modes.
    Also handles scheduling POIs from the ideas list.

    Allowed modes (camelCase from frontend):
    - allDay: true
    - startTime and endTime both provided (HH:MM)
    - singleTime: "HH:MM" (infer endTime from role/pacing defaults)

    Reject payloads with only one of startTime/endTime.
    """
    try:
        user_id = get_optional_user_id(authorization)

        # Convert payload from camelCase to snake_case
        payload = transform_frontend_to_canonical(payload)

        poi_id = payload.get("poi_id")
        day_index = payload.get("day_index")
        if not poi_id or day_index is None:
            raise HTTPException(
                status_code=400, detail="poiId and dayIndex are required"
            )

        # Load with ownership verification for logged-in users
        data = load_itinerary_with_auth(itin_id, user_id)
        if "plan" not in data or "days" not in data["plan"]:
            raise HTTPException(status_code=400, detail="Invalid itinerary structure")

        days = data["plan"]["days"]
        day_index = int(day_index)
        if day_index < 0 or day_index >= len(days):
            raise HTTPException(status_code=400, detail="Invalid dayIndex")

        # Find and remove POI from current location (any day)
        poi_stop = None
        from_ideas = False
        for day in days:
            stops = day.get("stops", [])
            for stop in stops:
                if stop.get("poi_id") == poi_id:
                    poi_stop = stop
                    day["stops"] = [s for s in stops if s.get("poi_id") != poi_id]
                    break
            if poi_stop:
                break

        # If not found in days, check ideas list
        if not poi_stop:
            ideas = data.get("meta", {}).get("ideas", [])
            for idea in ideas:
                if idea.get("id") == poi_id:
                    from_ideas = True
                    # Create a stop from the idea
                    poi_stop = {
                        "poi_id": idea.get("id"),
                        "name": idea.get("name"),
                        "role": idea.get("role") or "attraction",
                        "location": idea.get("location"),
                        "themes": idea.get("themes", []),
                        "images": idea.get("images"),
                        "coordinates": idea.get("coordinates"),
                    }
                    # Remove from ideas list
                    data["meta"]["ideas"] = [i for i in ideas if i.get("id") != poi_id]
                    break

        if not poi_stop:
            raise HTTPException(status_code=404, detail="POI not found in itinerary or ideas")

        # Mode validation
        all_day = bool(payload.get("all_day", False))
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        single_time = payload.get("single_time")

        # Get pacing and role for duration calculation
        pacing = data.get("meta", {}).get("preferences", {}).get("pacing", "balanced")
        role = poi_stop.get("role", "attraction")
        try:
            duration_min = int(
                vrp_config.service_time_min.get(role, {}).get(pacing, 60)
            )
        except Exception:
            duration_min = 60

        if all_day:
            # For ideas being scheduled, assign a default time based on existing stops
            target_day = days[day_index]
            existing_stops = target_day.get("stops", [])

            # Find the last stop with a depart time
            last_depart = None
            for stop in reversed(existing_stops):
                if stop.get("depart"):
                    last_depart = stop["depart"]
                    break

            if last_depart and from_ideas:
                # Schedule after the last stop
                try:
                    h, m = map(int, str(last_depart).split(":"))
                    start_min = h * 60 + m + 30  # 30 min buffer after last stop
                    end_min = start_min + duration_min
                    end_min = min(end_min, 22 * 60)  # Cap at 10 PM
                    start_min = min(start_min, end_min - 30)  # Ensure at least 30 min
                    poi_stop["arrival"] = f"{start_min // 60:02d}:{start_min % 60:02d}"
                    poi_stop["start_service"] = poi_stop["arrival"]
                    poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
                except Exception:
                    # Default to 10 AM - 11 AM if parsing fails
                    poi_stop["arrival"] = "10:00"
                    poi_stop["start_service"] = "10:00"
                    poi_stop["depart"] = f"{10 + duration_min // 60:02d}:{duration_min % 60:02d}"
            elif from_ideas:
                # No existing stops, default to 10 AM
                end_min = 10 * 60 + duration_min
                poi_stop["arrival"] = "10:00"
                poi_stop["start_service"] = "10:00"
                poi_stop["depart"] = f"{end_min // 60:02d}:{end_min % 60:02d}"
            else:
                # Existing POI being set to "all day" - clear times
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
                        detail="Invalid singleTime format; expected HH:MM",
                    )
            else:
                # No valid mode
                raise HTTPException(
                    status_code=400,
                    detail="Provide allDay, both start/end times, or singleTime",
                )

        # Add to target day at specific position or end
        target_day = days[day_index]
        target_day.setdefault("stops", [])
        
        # Check for target position
        target_position = payload.get("target_position")
        recalculate_times = payload.get("recalculate_times", True)
        
        if target_position is not None and isinstance(target_position, int):
            # Insert at specific position (respecting depot/hotel boundaries)
            stops = target_day["stops"]
            min_pos = 1 if stops and stops[0].get("role") in ("depot", "hotel", "accommodation") else 0
            max_pos = len(stops) - 1 if stops and stops[-1].get("role") in ("depot", "hotel", "accommodation") else len(stops)
            insert_pos = max(min_pos, min(target_position, max_pos))
            target_day["stops"].insert(insert_pos, poi_stop)
            
            # Recalculate times for entire day to respect sequence
            if recalculate_times:
                _recalculate_day_times(target_day, pacing)
        else:
            # Append and sort by time (legacy behavior)
            target_day["stops"].append(poi_stop)
            _sort_day_stops_by_time(target_day)
        
        _recompute_day_metrics(target_day)

        save_itinerary(itin_id, data, user_id)
        # logger.info(f"Scheduled POI {poi_id} in itinerary {itin_id} (from_ideas={from_ideas})")
        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to schedule POI in itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}/poi/{poi_id}")
def delete_poi_from_itinerary(
    itin_id: str, poi_id: str, authorization: Optional[str] = Header(None)
):
    """
    Remove a POI from the itinerary.

    Args:
        itin_id: Itinerary identifier
        poi_id: POI identifier to remove
        authorization: Optional Bearer token for ownership verification

    Returns:
        Updated itinerary data in camelCase format
    """
    try:
        user_id = get_optional_user_id(authorization)
        # Load with ownership verification for logged-in users
        data = load_itinerary_with_auth(itin_id, user_id)

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

        save_itinerary(itin_id, data, user_id)
        # logger.info(f"Deleted POI {poi_id} from itinerary {itin_id}")

        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete POI from itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/itinerary/{itin_id}/idea/{idea_id}")
def delete_idea_from_itinerary(
    itin_id: str, idea_id: str, authorization: Optional[str] = Header(None)
):
    """
    Remove an idea from the itinerary's ideas list.

    Args:
        itin_id: Itinerary identifier
        idea_id: Idea/POI identifier to remove
        authorization: Optional Bearer token for ownership verification

    Returns:
        Updated itinerary data in camelCase format
    """
    try:
        user_id = get_optional_user_id(authorization)
        # Load with ownership verification for logged-in users
        data = load_itinerary_with_auth(itin_id, user_id)

        if "meta" not in data:
            data["meta"] = {}
        if "ideas" not in data["meta"]:
            data["meta"]["ideas"] = []

        # Remove from ideas list
        ideas = data["meta"]["ideas"]
        original_len = len(ideas)
        data["meta"]["ideas"] = [i for i in ideas if i.get("id") != idea_id]

        if len(data["meta"]["ideas"]) == original_len:
            raise HTTPException(status_code=404, detail="Idea not found in itinerary")

        save_itinerary(itin_id, data, user_id)
        # logger.info(f"Deleted idea {idea_id} from itinerary {itin_id}")

        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete idea from itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/update-meta")
def update_itinerary_meta(
    itin_id: str, payload: dict, authorization: Optional[str] = Header(None)
):
    """
    Update itinerary metadata (dates, travelers, preferences, flags) and adjust plan days.

    Behavior:
    - If dates change and days increase: append empty days to plan.
    - If dates change and days decrease: move truncated day POIs to 'ideas' and trim days.

    Args:
        itin_id: Itinerary identifier
        authorization: Optional Bearer token for ownership verification
        payload (camelCase from frontend): {
            "dates": {...},
            "travelers": {...},
            "preferences": {...},
            "flags": {...}
        }

    Returns:
        Updated itinerary data in camelCase format

    Raises:
        HTTPException: 403 if not owner, 404 if not found, 400 for invalid payload, 500 for errors
    """
    try:
        user_id = get_optional_user_id(authorization)

        # Convert payload from camelCase to snake_case
        payload = transform_frontend_to_canonical(payload)

        # Load with ownership check if DB available
        if DB_STORAGE_AVAILABLE:
            data, error = load_itinerary_for_user(itin_id, user_id)
            if error == "forbidden":
                raise HTTPException(
                    status_code=403,
                    detail="You don't have permission to modify this itinerary",
                )
            if error == "not_found":
                # Fall through to file storage
                data = None
            if data is None:
                data = load_itinerary(itin_id)
        else:
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

        # Update hotels if provided
        if "hotels" in payload:
            data["meta"]["hotels"] = payload["hotels"]
            # logger.info(f"Updated hotels for itinerary {itin_id}")

        # Update mandatory POIs if provided
        if "mandatory_pois" in payload:
            data["meta"]["mandatory_pois"] = payload["mandatory_pois"]
            # logger.info(f"Updated mandatory_pois for itinerary {itin_id}")

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
                    days_list.append({"stops": []})
                # logger.info(f"Extended itinerary {itin_id} days: {current_days} -> {new_days}")
            else:
                # Move POIs from truncated days to ideas
                _ensure_ideas(data)
                moved = 0
                from app.api.pois import get_poi_by_id

                truncated = days_list[new_days:]
                for day in truncated:
                    for stop in day.get("stops", []):
                        poi_id = stop.get("poi_id")
                        # Strip _dayX suffix if present
                        base_poi_id = poi_id.rsplit("_day", 1)[0] if poi_id else None
                        if not base_poi_id:
                            continue
                        try:
                            res = get_poi_by_id(base_poi_id)
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
                # logger.info(f"Trimmed itinerary {itin_id} days: {current_days} -> {new_days}, moved {moved} POIs to ideas")

        # Recompute day labels (day number, date, weekday) based on updated dates
        if "dates" in payload and days_list:
            recompute_day_labels(days_list, data["meta"].get("dates"))

        # Persist updated itinerary (pass user_id to save to database)
        save_itinerary(itin_id, data, user_id)
        # logger.info(f"Updated metadata for itinerary {itin_id}")

        return transform_itinerary_response_to_frontend(data)

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Itinerary not found")
    except Exception as e:
        logger.exception(f"Failed to update itinerary metadata {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/itinerary/{itin_id}/add-poi")
def add_poi_to_itinerary(
    itin_id: str, payload: dict, authorization: Optional[str] = Header(None)
):
    """
    Add a POI to an itinerary's ideas list.

    Args:
        itin_id: Itinerary identifier
        payload (camelCase from frontend): {"poiId": str, "day": int (optional)}
        authorization: Optional Bearer token for ownership verification

    Returns:
        Updated itinerary data in camelCase format

    Raises:
        HTTPException: 404 if itinerary not found, 400 for invalid payload, 500 for errors
    """
    try:
        user_id = get_optional_user_id(authorization)

        # Convert payload from camelCase to snake_case
        payload = transform_frontend_to_canonical(payload)

        # Validate payload
        poi_id = payload.get("poi_id")
        if not poi_id:
            raise HTTPException(status_code=400, detail="poiId is required")

        # Load existing itinerary with ownership verification
        try:
            data = load_itinerary_with_auth(itin_id, user_id)
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
                # Also check if POI is already scheduled in any day
                poi_in_days = False
                for day in data.get("plan", {}).get("days", []):
                    for stop in day.get("stops", []):
                        if stop.get("poi_id") == poi_id:
                            poi_in_days = True
                            break
                    if poi_in_days:
                        break

                if poi_in_days:
                    logger.info(f"POI {poi_id} already scheduled in itinerary {itin_id}")
                else:
                    # Add POI to ideas
                    idea_item = {
                        "id": poi_details.get("id"),
                        "name": poi_details.get("name"),
                        "category": poi_details.get("category"),
                        "categories": poi_details.get("categories"),
                        "rating": poi_details.get("rating"),
                        "reviews_count": poi_details.get("reviews_count"),
                        "roles": poi_details.get("roles"),
                        "role": (poi_details.get("roles") or ["attraction"])[0],
                        "themes": poi_details.get("themes"),
                        "location": poi_details.get("location"),
                        "images": poi_details.get("images", [None])[0],
                        "coordinates": {
                            "lat": poi_details.get("latitude"),
                            "lng": poi_details.get("longitude"),
                        },
                    }
                    data["meta"]["ideas"].append(idea_item)

                    # Save updated itinerary
                    save_itinerary(itin_id, data, user_id)
                    # logger.info(f"Added POI {poi_id} to itinerary {itin_id}")
            else:
                logger.info(f"POI {poi_id} already in ideas for itinerary {itin_id}")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch POI details: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch POI details")

        return transform_itinerary_response_to_frontend(data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add POI to itinerary {itin_id}")
        raise HTTPException(status_code=500, detail=str(e))
