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
    role: Optional[str] = Query(None, regex="^(attractions|restaurants|hotels)$"),
):
    """
    Paginated POIs.
    - Uses DB paging (range) and returns total count.
    - Filters via poi_roles (array): attractions->attraction, restaurants->meal, hotels->accommodation.
    """
    try:
        supabase = get_supabase()

        # Base select with total count
        fields = "id, google_map_link, name, categories, address, website, phone, poi_roles, open_hours, review_count, review_rating, complete_address, descriptions, price_level, images"
        q = supabase.table("pois").select(
            fields, count="exact"
        )  # .select("*", count="exact")

        if role:
            role = UI_TO_ROLE[role]
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


@router.get("/search")
def search_pois(
    q: str = Query("", description="Search query"),
    limit: int = Query(settings.DEFAULT_LIMIT, ge=1, le=settings.MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """
    Full-text-ish search over name/description/address.
    - Paginates and returns total count.
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
        resp = supabase.rpc(
            "rpc_search_locations", {"p_query": q, "p_limit": limit}
        ).execute()

        data = resp.data or []

        return {"status": "success", "data": data}
    except Exception as e:
        logger.exception("Error searching locations")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pois/search-by-destination")
def search_pois_by_destination(
    destination: str = Query(..., description="Destination name"),
    roles: str = Query(
        ..., description="Comma-separated roles (accommodation, meal, attraction)"
    ),
    q: Optional[str] = Query(None, description="POI name search query"),
    limit: int = Query(5, ge=1, le=50),
):
    """
    Search POIs by destination and role(s).
    """
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
                "p_destination": destination,
                "p_roles": role_list,
                "p_query": q,
                "p_limit": limit,
            },
        ).execute()

        raw_data = resp.data or []

        # Transform to frontend format
        transformed = []
        for poi in raw_data:
            # Parse images - handle both string and array
            images = poi.get("images", [])
            if isinstance(images, str):
                # If it's a string, try to parse as JSON array
                try:
                    import json

                    images = json.loads(images)
                except:
                    images = [images] if images else []
            elif not isinstance(images, list):
                images = []

            # Parse themes - handle both string and array
            themes = poi.get("themes", [])
            if isinstance(themes, str):
                try:
                    import json

                    themes = json.loads(themes)
                except:
                    themes = [themes] if themes else []
            elif not isinstance(themes, list):
                themes = []

            # Get poi_roles and extract first role or use role field
            poi_roles = poi.get("poi_roles", [])
            if isinstance(poi_roles, str):
                try:
                    import json

                    poi_roles = json.loads(poi_roles)
                except:
                    poi_roles = [poi_roles] if poi_roles else []
            elif not isinstance(poi_roles, list):
                poi_roles = []

            role = poi_roles[0] if poi_roles else poi.get("role", "")

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
                    "role": role,
                    "poiRoles": poi_roles,
                    "openHours": poi.get("open_hours"),
                }
            )
        return {"status": "success", "data": transformed}
    except Exception as e:
        logger.exception("Error searching POIs by destination")
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
