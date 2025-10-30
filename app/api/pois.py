import random
import logging
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.db.supabase_client import get_supabase
from app.core.config import settings
from app.services.transformers import transform_supabase_poi

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pois"])

def get_random_pois(limit: int = 20, category: Optional[str] = None):
    """Fetch random POIs from Supabase"""
    try:
        supabase = get_supabase()
        response = supabase.table('pois').select('*').limit(10000).execute()
        
        if not response.data:
            return []
        
        pois = response.data
        
        # Filter by category
        if category and category in ['attractions', 'restaurants', 'hotels']:
            category_map = {
                'attractions': ['Tourist attraction', 'Attraction', 'Museum', 'Park', 'Beach', 
                               'Historical landmark', 'Art gallery', 'Zoo', 'Aquarium'],
                'restaurants': ['Restaurant', 'Cafe', 'Bar', 'Food court', 'Hawker centre',
                              'Bakery', 'Coffee shop'],
                'hotels': ['Hotel', 'Hostel', 'Resort', 'Guest house', 'Lodging', 'Motel']
            }
            
            filtered_pois = []
            for poi in pois:
                if poi.get('categories'):
                    poi_categories = poi['categories'] if isinstance(poi['categories'], list) else [poi['categories']]
                    for cat in poi_categories:
                        if any(filter_cat.lower() in str(cat).lower() for filter_cat in category_map[category]):
                            filtered_pois.append(poi)
                            break
            pois = filtered_pois
        
        if len(pois) > limit:
            pois = random.sample(pois, limit)
        
        return [transform_supabase_poi(poi) for poi in pois]
    
    except Exception as e:
        logger.error(f"Error fetching POIs: {e}")
        return []

@router.get("/pois")
def get_pois(
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    category: Optional[str] = Query(None, regex="^(attractions|restaurants|hotels)$")
):
    """Get random POIs"""
    pois = get_random_pois(limit=limit, category=category)
    
    return {
        "status": "success",
        "count": len(pois),
        "data": [poi.model_dump() for poi in pois]
    }

@router.get("/pois/{poi_id}")
def get_poi(poi_id: str):
    """Get a specific POI by ID"""
    try:
        supabase = get_supabase()
        response = supabase.table('pois').select('*').eq('id', poi_id).single().execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="POI not found")
        
        poi = transform_supabase_poi(response.data)
        
        return {
            "status": "success",
            "data": poi.model_dump()
        }
    except Exception as e:
        logger.error(f"Error fetching POI {poi_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search")
def search_pois(
    q: str = Query("", description="Search query"),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT)
):
    """Search POIs by name, category, or description"""
    try:
        supabase = get_supabase()
        
        if not q:
            return {
                "status": "success",
                "query": q,
                "count": 0,
                "data": []
            }
        
        response = supabase.table('pois').select('*').or_(
            f"name.ilike.%{q}%,descriptions.ilike.%{q}%,address.ilike.%{q}%"
        ).limit(limit).execute()
        
        if not response.data:
            return {
                "status": "success",
                "query": q,
                "count": 0,
                "data": []
            }
        
        pois = [transform_supabase_poi(poi) for poi in response.data]
        
        return {
            "status": "success",
            "query": q,
            "count": len(pois),
            "data": [poi.model_dump() for poi in pois]
        }
    except Exception as e:
        logger.error(f"Error searching POIs: {e}")
        raise HTTPException(status_code=500, detail=str(e))
