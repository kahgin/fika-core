from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.core.config import settings
from app.db.supabase_client import get_supabase
from app.services.transformers import transform_poi_to_frontend
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["pois"])

UI_TO_ROLE = {
    "attraction": "attraction",
    "restaurant": "meal",
    "hotel": "accommodation",
}


@router.get("/pois")
def list_pois(
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    role: Optional[str] = Query(None, regex="^(attraction|restaurant|hotel)$"),
    destination: Optional[str] = Query(None),
):
    try:
        supabase = get_supabase()
        roles = [UI_TO_ROLE[role]] if role else None

        resp = supabase.rpc(
            "rpc_search_pois",
            {
                "p_mode": "list",
                "p_destination": destination,
                "p_roles": roles,
                "p_query": None,
                "p_limit": limit,
                "p_offset": offset,
            },
        ).execute()

        data = resp.data or []

        # Extract total count from first row
        total = data[0]["total_count"] if data else 0

        # Transform POIs to frontend format (camelCase)
        pois = [transform_poi_to_frontend({k: v for k, v in p.items() if k != "total_count"}) for p in data]

        return {
            "status": "success",
            "count": total,
            "data": pois,
        }
    except Exception as e:
        logger.exception("Error listing POIs")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
def search_pois(
    q: str = Query("", description="Search query"),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    destination: Optional[str] = Query(None),
    role: Optional[str] = Query(None, regex="^(attraction|restaurant|hotel)$"),
):
    try:
        if not q.strip():
            return {"status": "success", "query": q, "count": 0, "data": []}

        supabase = get_supabase()
        roles = [UI_TO_ROLE[role]] if role else None

        resp = supabase.rpc(
            "rpc_search_pois",
            {
                "p_mode": "search",
                "p_destination": destination,
                "p_roles": roles,
                "p_query": q,
                "p_limit": limit,
                "p_offset": offset,
            },
        ).execute()

        data = resp.data or []

        # Extract total count from first row
        total = data[0]["total_count"] if data else 0

        # Transform POIs to frontend format (camelCase)
        pois = [transform_poi_to_frontend({k: v for k, v in p.items() if k != "total_count"}) for p in data]

        return {
            "status": "success",
            "query": q,
            "count": total,
            "data": pois,
        }
    except Exception as e:
        logger.exception("Error searching POIs")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pois/search-minimal")
def search_pois_minimal(
    destination: str = Query(..., description="Destination name"),
    roles: str = Query(..., description="Comma-separated roles"),
    q: Optional[str] = Query(None, description="POI name search query"),
    limit: int = Query(5, ge=1, le=50),
):
    try:
        if not destination or not roles:
            return {"status": "success", "data": []}

        role_list = [r.strip() for r in roles.split(",") if r.strip()]
        if not role_list:
            return {"status": "success", "data": []}

        supabase = get_supabase()
        resp = supabase.rpc(
            "rpc_search_pois",
            {
                "p_mode": "search_minimal",
                "p_destination": destination,
                "p_roles": role_list,
                "p_query": q,
                "p_limit": limit,
                "p_offset": 0,
            },
        ).execute()

        raw_data = resp.data or []
        transformed = []

        # Transform to frontend format (camelCase)
        for poi in raw_data:
            images = poi.get("images") or []
            themes = poi.get("themes") or []
            roles = poi.get("roles") or []

            transformed.append(
                {
                    "id": poi.get("id"),
                    "name": poi.get("name"),
                    "coordinates": {
                        "lat": poi.get("latitude"),
                        "lng": poi.get("longitude"),
                    },
                    "images": images,
                    "themes": themes,
                    "role": roles[0] if roles else "",
                    "roles": roles,
                    "openHours": poi.get("open_hours"),
                }
            )

        return {"status": "success", "data": transformed}
    except Exception as e:
        logger.exception("Error searching POIs minimal")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/locations/search")
def search_locations(
    q: str = Query("", description="Search query", min_length=3),
    limit: int = Query(12, ge=1, le=50),
):
    """
    Search locations (admin areas) by name.
    """
    try:
        if not q.strip() or len(q) < 3:
            return {"status": "success", "data": []}

        supabase = get_supabase()
        resp = supabase.rpc("rpc_search_locations", {"p_query": q, "p_limit": limit}).execute()

        data = resp.data or []

        return {"status": "success", "data": data}
    except Exception as e:
        logger.exception("Error searching locations")
        raise HTTPException(status_code=500, detail=str(e))


def get_poi_by_id(poi_id: str):
    """Helper function to get POI data by ID (for internal use)"""
    try:
        supabase = get_supabase()
        resp = supabase.table("pois").select("*").eq("id", poi_id).single().execute()
        if not resp.data:
            return None
        # Transform to frontend format (camelCase)
        poi = transform_poi_to_frontend(resp.data)
        return {"status": "success", "data": poi}
    except Exception as e:
        logger.error(f"Error fetching POI {poi_id}: {e}")
        return None


@router.get("/pois/{poi_id}")
def get_poi(poi_id: str):
    """Get a specific POI by ID"""
    result = get_poi_by_id(poi_id)
    if result is None:
        raise HTTPException(status_code=404, detail="POI not found")
    return result
