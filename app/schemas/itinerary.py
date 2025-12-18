from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import Any, Dict, List, Optional, Tuple

# NOTE: Canonical naming convention is snake_case across API, backend, and DB.
# All internal data uses snake_case. Conversion to camelCase happens only
# at API response boundaries via transformers.


class Coordinates(BaseModel):
    """Geographic coordinates for a POI (shared across services)."""

    lat: float
    lng: float


class POI(BaseModel):
    """Canonical POI schema consumed by MAUT, Pipeline, and CVRPTW (snake_case only)."""

    id: str
    name: str
    roles: List[str] = []
    area_name: Optional[str] = None
    category: Optional[str] = None
    categories: Optional[List[str]] = None
    themes: Optional[List[str]] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    images: List[str] = []
    coordinates: Optional[Coordinates] = None
    open_hours: Optional[Dict[str, Any]] = None
    price_level: Optional[int] = None
    maut_score: Optional[float] = None

    @field_validator("images", mode="before")
    @classmethod
    def filter_none_images(cls, v):
        """Filter out None values from images list."""
        if v is None:
            return []
        return [img for img in v if img is not None]


class DatesFlexible(BaseModel):
    """Flexible dates (days count). Canonical snake_case."""

    type: str = "flexible"
    days: int
    preferred_month: Optional[str] = None


class DatesSpecific(BaseModel):
    """Specific date range (inclusive). Canonical snake_case."""

    type: str = "specific"
    start_date: str
    end_date: str


class DestinationSpec(BaseModel):
    """User-specified destination entry for multi-dest requests (canonical)."""

    city: str
    days: Optional[int] = None
    dates: Optional[Dict[str, Any]] = None


class ItineraryRequest(BaseModel):
    """Create itinerary request payload (ingress, canonical snake_case)."""

    destinations: Optional[List[DestinationSpec]] = None
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

    time_type: 'specific' | 'all_day' | 'any_time' (default: any_time)
    day: 1-based day index; optional
    window: [start_hh:mm, end_hh:mm]; optional (for time_type='specific')
    all_day: bool; optional (for time_type='all_day')

    Presence in the mapping marks a POI as mandatory even if all fields are None/default.
    """

    poi_id: str
    time_type: str = "any_time"
    day: Optional[int] = None
    window: Optional[Tuple[str, str]] = None
    all_day: Optional[bool] = None


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
