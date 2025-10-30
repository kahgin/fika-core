from fastapi import APIRouter, HTTPException
from app.schemas.itinerary import ItineraryRequest, ItineraryResponse, POI
from app.services.maut import score_all_pois
from app.services.cvrptw_solver import optimize_route
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["itinerary"])

@router.post("/itinerary/optimize")
def optimize_itinerary(request: ItineraryRequest) -> ItineraryResponse:
    """Optimize itinerary using MAUT scoring and CVRPTW routing"""
    try:
        pois = request.pois
        preferences = request.preferences
        
        if not pois:
            raise HTTPException(status_code=400, detail="No POIs provided")
        
        # Score POIs
        scores = score_all_pois(pois, preferences)
        logger.info(f"POI scores: {scores}")
        
        # Optimize route
        route_order, total_distance = optimize_route(pois)
        logger.info(f"Optimized route: {route_order}, distance: {total_distance}km")
        
        # Reorder POIs based on route
        poi_map = {p.id: p for p in pois}
        optimized_pois = [poi_map[poi_id] for poi_id in route_order if poi_id in poi_map]
        
        return ItineraryResponse(
            status="success",
            optimized_pois=optimized_pois,
            total_distance=round(total_distance, 2),
            total_time=int(total_distance * 10),  # Rough estimate: 10 mins per km
            route_order=route_order
        )
    
    except Exception as e:
        logger.error(f"Error optimizing itinerary: {e}")
        raise HTTPException(status_code=500, detail=str(e))
