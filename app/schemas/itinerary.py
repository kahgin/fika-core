from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple

# NOTE: Canonical naming convention is snake_case across API, backend, and DB.
# For backward compatibility inside the codebase, select fields accept camelCase
# aliases during model construction. All serialization uses snake_case.


class Coordinates(BaseModel):
    """Geographic coordinates for a POI (shared across services)."""

    lat: float
    lng: float


class POI(BaseModel):
    """Canonical POI schema consumed by MAUT, Pipeline, and CVRPTW (snake_case).

    Aliases provided for legacy internal constructors:
    - review_count <- reviewCount
    - open_hours <- openHours
    - price_level <- priceLevel
    """

    id: str
    name: str
    poi_roles: List[str] = []
    category: Optional[str] = None
    categories: Optional[List[str]] = None
    themes: Optional[List[str]] = None
    rating: Optional[float] = None
    review_count: Optional[int] = Field(default=None, alias="reviewCount")
    images: List[str] = []
    coordinates: Optional[Coordinates] = None
    open_hours: Optional[Dict[str, Any]] = Field(default=None, alias="openHours")
    price_level: Optional[int] = Field(default=None, alias="priceLevel")

    model_config = {
        "populate_by_name": True,  # allow using snake_case field names explicitly
    }


class DatesFlexible(BaseModel):
    """Flexible dates (days count)."""

    type: str = "flexible"
    days: int
    preferredMonth: Optional[str] = None  # kept as-is if used externally


class DatesSpecific(BaseModel):
    """Specific date range (inclusive)."""

    type: str = "specific"
    startDate: str
    endDate: str


class DestinationSpec(BaseModel):
    """User-specified destination entry for multi-city requests.

    city: canonical city/destination name
    days: optional days to spend; if missing, system will allocate equally
    """

    city: str
    days: Optional[int] = None


class ItineraryRequest(BaseModel):
    """Create itinerary request payload (ingress, canonical snake_case)."""

    destination: Optional[str] = None  # legacy single destination
    destinations: Optional[List[DestinationSpec]] = None  # multi-destination
    dates: Dict[str, Any] = {}
    travelers: Dict[str, Any] = {}
    preferences: Dict[str, Any] = {}
    excluded_themes: Optional[List[str]] = None
    flags: Dict[str, Any] = {}
    seed_lon: Optional[float] = None
    seed_lat: Optional[float] = None


class ItineraryResponse(BaseModel):
    """MAUT response with flattened places used by downstream pipeline."""

    status: str
    places: List[POI]
    total_distance: float
    total_time: int
    route_order: List[str]
    meta: Dict[str, Any]


# Shared constraints and operations schemas (snake_case only)


class MandatoryPoiSpec(BaseModel):
    """Mandatory POI spec (shared by API → Solver adapter and CVRPTW).

    day: 1-based day index; optional
    window: [start_hh:mm, end_hh:mm]; optional
    Presence in the mapping marks a POI as mandatory even if both fields are None.
    """

    poi_id: str
    day: Optional[int] = None
    window: Optional[Tuple[str, str]] = None


class UserHotel(BaseModel):
    """User-provided hotel (used in multi-city pinning and depot selection)."""

    id: str
    name: str
    lat: float
    lon: float
    source: str = "user"


class ReorderItineraryRequest(BaseModel):
    """Reorder itinerary request (supports single_day or entire_trip scopes)."""

    scope: str = Field(default="single_day", description="single_day | entire_trip")
    day_index: Optional[int] = None  # required if scope==single_day
    ordered_poi_ids: List[str] = []
    moves: Optional[Dict[str, int]] = None  # poi_id -> target day_index
    options: Dict[str, Any] = {}


class SchedulePoiRequest(BaseModel):
    """Schedule POI request with strict input modes (snake_case only)."""

    poi_id: str
    day_index: int
    all_day: Optional[bool] = False
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    single_time: Optional[str] = None  # HH:MM; infer duration via role/pacing
