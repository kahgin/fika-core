from fastapi import APIRouter, HTTPException
from app.schemas.itinerary import ItineraryRequest, ItineraryResponse, POI
from app.services.cvrptw_solver import optimize_route
import logging, os, json, uuid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["itinerary"])

@router.post("/itinerary/optimize")
def optimize_itinerary(request: ItineraryRequest) -> ItineraryResponse:
    """Optimize itinerary using CVRPTW routing (legacy endpoint)."""
    try:
        pois = request.pois
        if not pois:
            raise HTTPException(status_code=400, detail="No POIs provided")

        # Optimize route
        route_order, total_distance = optimize_route(pois)
        poi_map = {p.id: p for p in pois}
        optimized_pois = [poi_map[poi_id] for poi_id in route_order if poi_id in poi_map]
        
        return ItineraryResponse(
            status="success",
            optimized_pois=optimized_pois,
            total_distance=round(total_distance, 2),
            total_time=int(total_distance * 10),
            route_order=route_order
        )
    except Exception as e:
        logger.error(f"Error optimizing itinerary: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/itinerary/create")
def create_itinerary(payload: dict):
    """
    Create a new itinerary from frontend form payload. For now, simulate MAUT output by
    reading tests/maut_output.json and returning it. Also persist to local storage
    under ./storage/itineraries/{chat_id}.json
    """
    try:
        # Simulate generation using sample MAUT output
        sample_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'tests', 'maut_output.json')
        with open(sample_path, 'r', encoding='utf-8') as f:
            maut_output = json.load(f)
        chat_id = str(uuid.uuid4())
        result = {
            "chat_id": chat_id,
            "status": "success",
            "meta": {
                "destination": payload.get("destination"),
                "dates": payload.get("dates"),
                "travelers": payload.get("travelers"),
                "preferences": payload.get("preferences"),
            },
            "maut": maut_output
        }
        # Persist locally to mimic DB
        storage_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'storage', 'itineraries')
        os.makedirs(storage_dir, exist_ok=True)
        with open(os.path.join(storage_dir, f"{chat_id}.json"), 'w', encoding='utf-8') as fw:
            json.dump(result, fw, ensure_ascii=False, indent=2)
        return result
    except Exception as e:
        logger.exception("Failed to create itinerary")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/itinerary/{chat_id}")
def get_itinerary(chat_id: str):
    try:
        storage_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'storage', 'itineraries')
        path = os.path.join(storage_dir, f"{chat_id}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Itinerary not found")
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to load itinerary")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/itinerary/{chat_id}")
def delete_itinerary(chat_id: str):
    try:
        storage_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'storage', 'itineraries')
        path = os.path.join(storage_dir, f"{chat_id}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Itinerary not found")
        os.remove(path)
        return {"status": "deleted", "chat_id": chat_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete itinerary")
        raise HTTPException(status_code=500, detail=str(e))
