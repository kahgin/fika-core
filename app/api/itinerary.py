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

        # 4. Extract hotel information if exist (still on testing mode)
        places = maut_output.get("places", [])
        accommodations = [
            p for p in places if "accommodation" in p.get("poi_roles", [])
        ]

        if accommodations:
            hotel_poi = accommodations[0]
            coords = hotel_poi.get("coordinates") or {}
            hotel = {
                "id": hotel_poi["id"],
                "name": hotel_poi["name"],
                "lat": coords.get("lat") or hotel_poi.get("latitude"),
                "lon": coords.get("lng") or hotel_poi.get("longitude"),
            }
            logger.info(f"Using accommodation from MAUT: {hotel['name']}")

        # 5. Run full pipeline (CVRPTW + ACO)
        pipeline_output = run_full_pipeline(
            maut_output=maut_output,
            hotel=hotel,
            pacing=maut_request.get("pacing", "balanced"),
            mandatory=None,
            time_limit_sec=20,
            use_aco=True,  # Enable ACO optimization
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
