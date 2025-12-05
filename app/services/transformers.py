from typing import Any, Dict, Optional
from datetime import date
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Frontend → Backend Transformation


def calculate_num_days(payload: Dict[str, Any]) -> int:
    """
    Calculate trip duration from dates object (1-10 days).

    Handles flexible (days field) and specific (startDate/endDate) date types.
    Uses datetime for automatic leap year and month boundary handling.
    """

    dates = payload.get("dates", {})
    if not isinstance(dates, dict):
        return 3

    date_type = dates.get("type")

    # Flexible dates: use days field directly
    if date_type == "flexible":
        try:
            return max(1, min(10, int(dates.get("days", 3))))
        except (ValueError, TypeError):
            return 3

    # Specific dates: calculate from startDate to endDate (inclusive)
    if date_type == "specific":
        try:
            start = date.fromisoformat(dates["startDate"].split("T")[0])
            end = date.fromisoformat(dates["endDate"].split("T")[0])
            return max(1, min(10, (end - start).days + 1))
        except (KeyError, ValueError, AttributeError):
            return 3

    return 3


def transform_frontend_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform frontend CreateItineraryPayload to internal MAUT request format.

    Transformations:
    - Extract and validate destination
    - Calculate num_days from dates if needed
    - Derive flags from travelers
    - Map preferences to internal format
    - Merge explicit flags with derived flags

    Args:
        payload: Frontend CreateItineraryPayload

    Returns:
        Internal MAUT request dict with normalized fields
    """
    preferences = payload.get("preferences", {})
    explicit_flags = payload.get("flags", {})
    dietary_restrictions = payload.get("dietary_restrictions", [])
    excluded_themes = payload.get("excluded_themes", [])

    # Merge explicit flags with derived flags (explicit takes precedence)
    flags = {
        "wheelchair_accessible": bool(
            explicit_flags.get("wheelchair_accessible", False)
        ),
        "is_muslim": bool(explicit_flags.get("is_muslim", False)),
        "kids_friendly": bool(explicit_flags.get("kids_friendly", False)),
        "pets_friendly": bool(explicit_flags.get("pets_friendly", False)),
    }

    if explicit_flags.get("is_muslim", False):
        # Only add "halal" if it's not already in dietary restrictions
        # and "halal" explicitly set to false
        halal_explicitly_false = any(
            isinstance(item, dict) and item.get("halal") is False
            for item in dietary_restrictions
        )
        if "halal" not in dietary_restrictions and not halal_explicitly_false:
            dietary_restrictions.append("halal")
        # Only add "nightlife" if it's not already in excluded themes
        if "nightlife" not in excluded_themes:
            excluded_themes.append("nightlife")

    # Build internal request
    return {
        "destination": payload.get("destination", "Singapore"),
        "num_days": calculate_num_days(payload),
        "budget_tier": preferences.get("budget", "sensible"),
        "pacing": preferences.get("pacing", "balanced"),
        "interest_themes": preferences.get("interests", []),
        "excluded_themes": excluded_themes,
        "dietary_restrictions": dietary_restrictions,
        "flags": flags,
        "seed_lon": payload.get("seed_lon"),
        "seed_lat": payload.get("seed_lat"),
        "excluded_themes": payload.get("excluded_themes"),
    }


# Backend → Frontend Transformation


def transform_poi_to_frontend(poi: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform internal POI format to frontend format.

    Field mappings:
    - review_rating → rating
    - review_count → reviewCount
    - poi_roles → roles
    - price_level → priceLevel
    - open_hours → openHours
    - complete_address → location (derived from city or country)

    Args:
        poi: Internal POI dict from MAUT service

    Returns:
        Frontend-formatted POI dict
    """
    # Extract coordinates
    coords = None
    if poi.get("coordinates"):
        coords = poi["coordinates"]

    # Get category (first from categories array or single category field)
    category = None
    if poi.get("categories") and len(poi["categories"]) > 0:
        category = poi["categories"][0]
    elif poi.get("category"):
        category = poi["category"]

    # Derive location from complete_address
    location = None
    complete_addr = poi.get("complete_address")
    if isinstance(complete_addr, dict):
        # Priority: city > country
        location = complete_addr.get("city") or complete_addr.get("country")

    return {
        "id": poi.get("id"),
        "name": poi.get("name"),
        "category": category,
        "categories": poi.get("categories", [category] if category else []),
        "rating": poi.get("review_rating") or poi.get("rating"),
        "reviewCount": poi.get("review_count") or poi.get("reviewCount"),
        "location": location,
        "images": poi.get("images", []),
        "description": poi.get("description") or poi.get("descriptions"),
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("googleMapsUrl") or poi.get("google_map_link"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "openHours": poi.get("open_hours"),
        "priceLevel": poi.get("price_level") or poi.get("priceLevel"),
        "roles": poi.get("poi_roles", []),
        "themes": poi.get("images", []),
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

    # Build plan structure
    return {
        "status": output.get("status", "ok"),
        "items": items,
        "total_distance": output.get("total_distance", 0.0),
        "total_time": output.get("total_time", 0),
        "route_order": output.get("route_order", []),
        "meta": output.get("meta", {}),
    }


# Validation Helpers


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
