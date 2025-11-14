from typing import Any, Dict, Optional
from datetime import datetime
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# Frontend → Backend Transformation
# ============================================================================


def derive_flags_from_travelers(travelers: Dict[str, Any]) -> Dict[str, bool]:
    """
    Derive boolean flags from traveler counts.

    Logic:
    - children >= 1 → has_child (triggers kids_friendly scoring)
    - pets >= 1 → has_pets (triggers pets_friendly scoring)

    Args:
        travelers: Dict with adults, children, pets counts

    Returns:
        Dict of boolean flags for MAUT scoring
    """
    children = travelers.get("children", 0) or 0
    pets = travelers.get("pets", 0) or 0

    return {
        "has_child": children >= 1,
        "has_pets": pets >= 1,
    }


def calculate_num_days(payload: Dict[str, Any]) -> int:
    """
    Calculate trip duration from dates or use provided num_days.

    Priority:
    1. payload.num_days (if provided)
    2. dates.days (for flexible dates from frontend)
    3. Calculate from dates.startDate and dates.endDate (for specific dates)
    4. Default to 3 days

    Args:
        payload: Frontend payload with dates and/or num_days

    Returns:
        Number of days for the trip (minimum 1)
    """
    # Check if num_days already provided
    if payload.get("num_days"):
        return max(1, int(payload["num_days"]))

    # Try to get from dates object
    dates = payload.get("dates", {})
    if isinstance(dates, dict):
        # For flexible dates: dates.days
        if dates.get("days"):
            return max(1, int(dates["days"]))

        # For specific dates: calculate from startDate and endDate
        if dates.get("type") == "specific":
            try:
                start = dates.get("startDate")
                end = dates.get("endDate")
                if start and end:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    diff_days = (end_dt - start_dt).days + 1
                    return max(1, diff_days)
            except Exception as e:
                logger.warning(f"Failed to calculate num_days from dates: {e}")

    # Default fallback
    return 3


def transform_frontend_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform frontend CreateItineraryPayload to internal MAUT request format.

    Transformations:
    - Extract and validate destination
    - Calculate num_days from dates if needed
    - Derive flags from travelers (children → has_child, pets → has_pets)
    - Map preferences to internal format
    - Merge explicit flags with derived flags

    Args:
        payload: Frontend CreateItineraryPayload

    Returns:
        Internal MAUT request dict with normalized fields

    Example:
        Input:
        {
            "destination": "Singapore",
            "dates": {"type": "specific", "startDate": "2024-01-01", "endDate": "2024-01-03"},
            "travelers": {"adults": 2, "children": 1, "pets": 0},
            "preferences": {"budget": "sensible", "pacing": "balanced", "interests": ["shopping", "food_culinary"]}
        }

        Output:
        {
            "destination": "Singapore",
            "num_days": 3,
            "budget_tier": "sensible",
            "pacing": "balanced",
            "interest_themes": ["shopping", "food_culinary"],
            "flags": {
                "has_child": True,
                "has_pets": False,
                "wheelchair_accessible": False,
                "is_muslim": False,
                "exclude_nightlife": False
            },
            ...
        }
    """
    travelers = payload.get("travelers", {})
    preferences = payload.get("preferences", {})
    explicit_flags = payload.get("flags", {})

    # Derive flags from travelers
    derived_flags = derive_flags_from_travelers(travelers)

    # Merge explicit flags with derived flags (explicit takes precedence)
    flags = {
        **derived_flags,
        "wheelchair_accessible": bool(
            explicit_flags.get("wheelchair_accessible", False)
        ),
        "is_muslim": bool(explicit_flags.get("is_muslim", False)),
        "exclude_nightlife": bool(explicit_flags.get("exclude_nightlife", False)),
    }

    # Build internal request
    return {
        "destination": payload.get("destination", "Singapore"),
        "num_days": calculate_num_days(payload),
        "budget_tier": preferences.get("budget", "sensible"),
        "pacing": preferences.get("pacing", "balanced"),
        "interest_themes": preferences.get("interests", []),
        "dietary_restrictions": payload.get("dietary_restrictions", []),
        "flags": flags,
        "seed_lon": payload.get("seed_lon"),
        "seed_lat": payload.get("seed_lat"),
        "excluded_themes": payload.get("excluded_themes"),
    }


# ============================================================================
# Backend → Frontend Transformation
# ============================================================================


def transform_poi_to_frontend(poi: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform internal POI format to frontend format.

    Field mappings:
    - review_rating → rating
    - review_count → reviewCount
    - poi_roles → roles
    - price_level → priceLevel
    - open_hours → hours, openHours

    Args:
        poi: Internal POI dict from MAUT service

    Returns:
        Frontend-formatted POI dict
    """
    # Extract coordinates
    coords = None
    if poi.get("coordinates"):
        coords = poi["coordinates"]
    elif poi.get("latitude") is not None and poi.get("longitude") is not None:
        coords = {"lat": float(poi["latitude"]), "lng": float(poi["longitude"])}

    # Get category (first from categories array or single category field)
    category = None
    if poi.get("categories") and len(poi["categories"]) > 0:
        category = poi["categories"][0]
    elif poi.get("category"):
        category = poi["category"]

    return {
        "id": poi.get("id"),
        "name": poi.get("name"),
        "category": category,
        "categories": poi.get("categories", [category] if category else []),
        "rating": poi.get("review_rating") or poi.get("rating"),
        "reviews": poi.get("review_count") or poi.get("reviewCount"),
        "reviewCount": poi.get("review_count") or poi.get("reviewCount"),
        "location": poi.get("location"),
        "images": poi.get("images", []),
        "description": poi.get("description") or poi.get("descriptions"),
        "latitude": coords["lat"] if coords else None,
        "longitude": coords["lng"] if coords else None,
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("googleMapsUrl") or poi.get("google_map_link"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "hours": poi.get("open_hours") or poi.get("hours"),
        "openHours": poi.get("open_hours") or poi.get("hours"),
        "price_level": poi.get("price_level") or poi.get("priceLevel"),
        "priceLevel": poi.get("price_level") or poi.get("priceLevel"),
        "roles": poi.get("poi_roles", []),
    }


def transform_response_to_frontend(
    output: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Transform pipeline output to frontend plan format.

    Args:
        output: Output from service (status, places, meta, etc.)
        payload: Original frontend payload (for metadata)

    Returns:
        Frontend plan dict with transformed POIs
    """
    # Transform POIs
    items = []
    for poi in output.get("items") or output.get("places") or []:
        items.append(transform_poi_to_frontend(poi))

    # Build plan structure (selected_themes only in meta, not at root)
    return {
        "status": output.get("status", "ok"),
        "items": items,
        "total_distance": output.get("total_distance", 0.0),
        "total_time": output.get("total_time", 0),
        "route_order": output.get("route_order", []),
        "meta": output.get("meta", {}),
    }


# ============================================================================
# Validation Helpers
# ============================================================================


def validate_create_itinerary_payload(
    payload: Dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """
    Validate frontend payload before processing.

    Required fields:
    - destination (non-empty string)

    Args:
        payload: Frontend CreateItineraryPayload

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not payload.get("destination"):
        return False, "Destination is required"

    if not isinstance(payload.get("destination"), str):
        return False, "Destination must be a string"

    if not payload["destination"].strip():
        return False, "Destination cannot be empty"

    return True, None
