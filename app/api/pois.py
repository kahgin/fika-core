import logging
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.core.config import settings
from app.db.supabase_client import get_supabase
from app.services.transformers import transform_supabase_poi

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pois"])

UI_TO_ROLE = {
    "attractions": "attraction",
    "restaurants": "meal",
    "hotels": "accommodation",
}

def apply_common_ordering(q):
    # Highest rated first, tie-break by more reviews
    return q.order("review_rating", desc=True).order("review_count", desc=True)

@router.get("/pois")
def list_pois(
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None, regex="^(attractions|restaurants|hotels)$")
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

        pois = [transform_supabase_poi(p) for p in data]
        return {
            "status": "success",
            "count": total,
            "data": [p.model_dump() for p in pois],
        }
    except Exception as e:
        logger.exception("Error listing POIs")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pois/{poi_id}")
def get_poi(poi_id: str):
    """Get a specific POI by ID"""
    try:
        supabase = get_supabase()
        resp = supabase.table("pois").select("*").eq("id", poi_id).single().execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="POI not found")
        poi = transform_supabase_poi(resp.data)
        return {"status": "success", "data": poi.model_dump()}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching POI")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
def search_pois(
    q: str = Query("", description="Search query"),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """
    Full-text-ish search over name/description/address.
    - Paginates and returns total count.
    - Keeps UI simple (no category filter here). Tabs use /pois?category=...
    """
    try:
        if not q.strip():
            return {"status": "success", "query": q, "count": 0, "data": []}

        supabase = get_supabase()

        # NOTE: ilike across a few fields; you can add category token search later
        filt = f"name.ilike.%{q}%,descriptions.ilike.%{q}%,address.ilike.%{q}%"
        base = supabase.table("pois").select("*", count="exact").or_(filt)

        base = apply_common_ordering(base)
        start = offset
        end = offset + limit - 1
        resp = base.range(start, end).execute()

        data = resp.data or []
        total = resp.count or 0

        pois = [transform_supabase_poi(p) for p in data]
        return {
            "status": "success",
            "query": q,
            "count": total,
            "data": [p.model_dump() for p in pois],
        }
    except Exception as e:
        logger.exception("Error searching POIs")
        raise HTTPException(status_code=500, detail=str(e))