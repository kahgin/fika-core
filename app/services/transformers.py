from typing import Any, Dict, List
from datetime import date
from app.utils.logger import get_logger
from app.utils.naming import dict_to_camel_case

logger = get_logger(__name__)

# Role mapping constants
ROLE_DEPOT = "depot"  # Internal VRP role
ROLE_ACCOMMODATION = "accommodation"  # Canonical role for output

# Frontend role mapping - maps internal roles to UI-friendly roles
FRONTEND_ROLE_MAP = {
    "depot": "accommodation",  # Map depot to accommodation for frontend
    "accommodation": "accommodation",
    "attraction": "attraction",
    "meal": "meal",
}

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

    # Specific dates: calculate from start_date to end_date (inclusive)
    if date_type == "specific":
        try:
            raw_start = dates.get("start_date")
            raw_end = dates.get("end_date")
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
        cities = [d.get("city") for d in payload.get("destinations") if d.get("city")]
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
        "dates": payload.get("dates"),  # Pass through for day spec creation
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


def to_camel_case(snake_str: str) -> str:
    """Convert snake_case to camelCase."""
    components = snake_str.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def transform_poi_to_frontend(poi: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform internal POI format (snake_case) to frontend format (camelCase).

    Field mappings:
    - review_rating → rating
    - review_count → reviewCount
    - price_level → priceLevel
    - open_hours → openHours
    - complete_address → location (derived from city or country)

    Args:
        poi: Internal POI dict from MAUT service (snake_case)

    Returns:
        Frontend-formatted POI dict (camelCase)
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
        "reviewCount": poi.get("review_count"),
        "location": location,
        "images": poi.get("images", []),
        "roles": poi.get("roles", []),
        "themes": poi.get("themes", []),
        "description": poi.get("description") or poi.get("descriptions"),
        "coordinates": coords,
        "website": poi.get("website"),
        "googleMapsUrl": poi.get("google_map_link"),
        "address": poi.get("address"),
        "phone": poi.get("phone"),
        "openHours": poi.get("open_hours"),
        "priceLevel": poi.get("price_level"),
        # Friendliness booleans
        "kidsFriendly": poi.get("kids_friendly"),
        "petsFriendly": poi.get("pets_friendly"),
        # Dietary options
        "halalFood": poi.get("halal_food"),
        "veganOptions": poi.get("vegan_options"),
        "vegetarianOptions": poi.get("vegetarian_options"),
        # Accessibility
        "wheelchairAccessibleEntrance": poi.get("wheelchair_accessible_entrance"),
        "wheelchairAccessibleSeating": poi.get("wheelchair_accessible_seating"),
        "wheelchairAccessibleToilet": poi.get("wheelchair_accessible_toilet"),
        "wheelchairAccessibleCarPark": poi.get("wheelchair_accessible_car_park"),
    }


def map_stop_role_for_frontend(role: str) -> str:
    """
    Map internal stop role to frontend-compatible role.

    Args:
        role: Internal role string

    Returns:
        Frontend-compatible role string
    """
    return FRONTEND_ROLE_MAP.get(role, role)


def transform_stop_for_frontend(stop: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform a single stop dict for frontend consumption.

    Maps internal roles to frontend roles and ensures consistent field naming.

    Args:
        stop: Internal stop dict

    Returns:
        Frontend-compatible stop dict
    """
    stop_copy = stop.copy()

    # Map role to frontend role
    if "role" in stop_copy:
        stop_copy["role"] = map_stop_role_for_frontend(stop_copy["role"])

    return stop_copy


def transform_day_for_frontend(day: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform a single day dict for frontend consumption.

    Transforms all stops within the day.

    Args:
        day: Internal day dict

    Returns:
        Frontend-compatible day dict
    """
    day_copy = day.copy()

    # Transform stops
    if "stops" in day_copy:
        day_copy["stops"] = [
            transform_stop_for_frontend(stop) for stop in day_copy["stops"]
        ]

    return day_copy


def transform_days_for_frontend(days: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Transform all days for frontend consumption.

    Args:
        days: List of internal day dicts

    Returns:
        List of frontend-compatible day dicts
    """
    return [transform_day_for_frontend(day) for day in days]


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


def transform_itinerary_response_to_frontend(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform full itinerary response to frontend format (camelCase).

    This is the main function to use when returning itinerary data to frontend.
    It converts all keys to camelCase recursively.

    Args:
        data: Internal itinerary data with snake_case keys

    Returns:
        Frontend-compatible data with camelCase keys
    """
    return dict_to_camel_case(data)
