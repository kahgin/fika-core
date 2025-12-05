from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class Coordinates(BaseModel):
    lat: float
    lng: float


class POI(BaseModel):
    id: str
    name: str
    poi_roles: List[str] = []
    category: Optional[str] = None
    categories: List[str] = []
    themes: Optional[List[str]] = None
    rating: Optional[float] = None
    reviewCount: Optional[int] = None
    images: List[str] = []
    coordinates: Optional[Coordinates] = None
    openHours: Optional[Dict[str, Any]] = None
    priceLevel: Optional[int] = None


class DatesFlexible(BaseModel):
    type: str = "flexible"
    days: int
    preferredMonth: Optional[str] = None


class DatesSpecific(BaseModel):
    type: str = "specific"
    startDate: str
    endDate: str


class ItineraryRequest(BaseModel):
    destination: str
    dates: Dict[str, Any] = {}
    travelers: Dict[str, Any] = {}
    preferences: Dict[str, Any] = {}
    excluded_themes: Optional[List[str]] = None
    flags: Dict[str, Any] = {}
    seed_lon: Optional[float] = None
    seed_lat: Optional[float] = None


class ItineraryResponse(BaseModel):
    status: str
    places: List[POI]
    total_distance: float
    total_time: int
    route_order: List[str]
    meta: Dict[str, Any]
