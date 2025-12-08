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
            raw_start = dates.get("startDate") or dates.get("start_date")
            raw_end = dates.get("endDate") or dates.get("end_date")
            start = date.fromisoformat(str(raw_start).split("T")[0])
            end = date.fromisoformat(str(raw_end).split("T")[0])
            return max(1, min(10, (end - start).days + 1))
        except (KeyError, ValueError, AttributeError, TypeError):
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
    raw_flags = payload.get("flags", {})

    # Handle dietary_restrictions as either string or list
    dietary_restrictions_raw = payload.get("dietary_restrictions", [])
    if isinstance(dietary_restrictions_raw, str):
        dietary_restrictions = (
            [dietary_restrictions_raw]
            if dietary_restrictions_raw and dietary_restrictions_raw != "none"
            else []
        )
    else:
        dietary_restrictions = (
            dietary_restrictions_raw
            if isinstance(dietary_restrictions_raw, list)
            else []
        )

    # Flags normalization for is_muslim
    user_excluded = payload.get("excluded_themes")
    if isinstance(user_excluded, list):
        excluded_themes = list(dict.fromkeys(user_excluded))
        if raw_flags.get("is_muslim") and "nightlife" not in excluded_themes:
            excluded_themes.append("nightlife")
    else:
        excluded_themes = ["nightlife"] if raw_flags.get("is_muslim") else []

    if raw_flags.get("is_muslim"):
        halal_explicitly_false = any(
            isinstance(item, dict) and item.get("halal") is False
            for item in dietary_restrictions
        )
        if "halal" not in dietary_restrictions and not halal_explicitly_false:
            dietary_restrictions.append("halal")

    # Compute base destination (multi-destination: pick first city)
    base_destination = payload.get("destination")
    if isinstance(payload.get("destinations"), list) and payload.get("destinations"):
        cities = [
            d.get("city") or d.get("name") or d.get("destination")
            for d in payload.get("destinations")
            if (d.get("city") or d.get("name") or d.get("destination"))
        ]
        if cities:
            base_destination = cities[0]
            logger.info(
                f"Multi-destination request: cities={cities}, base_destination={base_destination}"
            )
    if not base_destination:
        base_destination = "Singapore"

    # Build internal request
    return {
        "destination": base_destination,
        "num_days": calculate_num_days(payload),
        "budget_tier": preferences.get("budget", "sensible"),
        "pacing": preferences.get("pacing", "balanced"),
        "interest_themes": preferences.get("interests", []),
        "excluded_themes": excluded_themes,
        "dietary_restrictions": dietary_restrictions,
        "flags": raw_flags,
        "seed_lon": payload.get("seed_lon"),
        "seed_lat": payload.get("seed_lat"),
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

    # Get category
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
        "roles": poi.get("poi_roles", []),
        "poiRoles": poi.get("poi_roles", []),
        "themes": poi.get("themes", []),
        "description": poi.get("description") or poi.get("descriptions"),
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("googleMapsUrl") or poi.get("google_map_link"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "openHours": poi.get("open_hours"),
        "priceLevel": poi.get("price_level") or poi.get("priceLevel"),
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

    Accepts either:
    - destinations: list[{city: str, days?: int}]
    - OR legacy destination: non-empty string

    Returns a clear error message listing invalid fields.
    """
    errors: list[str] = []

    dest_list = (
        payload.get("destinations")
        if isinstance(payload.get("destinations"), list)
        else None
    )
    if dest_list is not None and len(dest_list) > 0:
        for idx, d in enumerate(dest_list):
            city = d.get("city") or d.get("name") or d.get("destination")
            if not city or not isinstance(city, str) or not city.strip():
                errors.append(
                    f"destinations[{idx}].city is required and must be a non-empty string"
                )
            if d.get("days") is not None:
                try:
                    iv = int(d.get("days"))
                    if iv <= 0:
                        errors.append(f"destinations[{idx}].days must be > 0")
                except Exception:
                    errors.append(f"destinations[{idx}].days must be an integer")
    else:
        # Legacy single destination path
        if "destination" not in payload:
            errors.append("destination is required when destinations is not provided")
        elif not isinstance(payload.get("destination"), str):
            errors.append("destination must be a string")
        elif not payload.get("destination", "").strip():
            errors.append("destination cannot be empty")

    if errors:
        msg = "Invalid itinerary payload: " + "; ".join(errors)
        logger.warning(msg)
        return False, msg

    return True, None
