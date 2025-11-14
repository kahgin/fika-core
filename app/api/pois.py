from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.core.config import settings
from app.db.supabase_client import get_supabase
from app.services.transformers import transform_poi_to_frontend
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["pois"])

UI_TO_ROLE = {
    "attractions": "attraction",
    "restaurants": "meal",
    "hotels": "accommodation",
}


def apply_common_ordering(q):
    # Highest rated first, tie-break by more reviews
    return q.order("review_count", desc=True).order("review_rating", desc=True)


@router.get("/pois")
def list_pois(
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None, regex="^(attractions|restaurants|hotels)$"),
):
    """
    Paginated POIs (optionally filtered by UI category tab).
    - Uses DB paging (range) and returns total count.
    - Filters via poi_roles (array): attractions->attraction, restaurants->meal, hotels->accommodation.
    """
    try:
        supabase = get_supabase()

        # Base select with total count
        q = supabase.table("pois").select("*", count="exact")

        # Optional category (via poi_roles)
        if category:
            role = UI_TO_ROLE[category]
            # 'contains' with a single-element list checks array overlap
            q = q.contains("poi_roles", [role])

        # Ordering + paging
        q = apply_common_ordering(q)
        start = offset
        end = offset + limit - 1
        resp = q.range(start, end).execute()

        data = resp.data or []
        total = resp.count or 0

        pois = [transform_poi_to_frontend(p) for p in data]
        return {
            "status": "success",
            "count": total,
            "data": pois,
        }
    except Exception as e:
        logger.exception("Error listing POIs")
        raise HTTPException(status_code=500, detail=str(e))


def get_poi_by_id(poi_id: str):
    """Helper function to get POI data by ID (for internal use)"""
    try:
        supabase = get_supabase()
        resp = supabase.table("pois").select("*").eq("id", poi_id).single().execute()
        if not resp.data:
            return None
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


@router.get("/search")
def search_pois(
    q: str = Query("", description="Search query"),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """
    Full-text-ish search over name/description/address.
    - Paginates and returns total count.
    - Keeps UI simple (no category filter here)
    """
    try:
        if not q.strip():
            return {"status": "success", "query": q, "count": 0, "data": []}

        supabase = get_supabase()

        # Note: ilike across a few fields
        filt = f"name.ilike.%{q}%,descriptions.ilike.%{q}%,address.ilike.%{q}%"
        base = supabase.table("pois").select("*", count="exact").or_(filt)

        base = apply_common_ordering(base)
        start = offset
        end = offset + limit - 1
        resp = base.range(start, end).execute()

        data = resp.data or []
        total = resp.count or 0

        pois = [transform_poi_to_frontend(p) for p in data]
        return {
            "status": "success",
            "query": q,
            "count": total,
            "data": pois,
        }
    except Exception as e:
        logger.exception("Error searching POIs")
        raise HTTPException(status_code=500, detail=str(e))
