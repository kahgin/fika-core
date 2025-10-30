from pydantic import BaseModel
from typing import List, Optional, Dict

class Coordinates(BaseModel):
    lat: float
    lng: float

class POI(BaseModel):
    id: str
    name: str
    category: str
    rating: float
    reviewCount: int
    location: str
    images: List[str] = []
    description: Optional[str] = None
    coordinates: Optional[Coordinates] = None
    website: Optional[str] = None
    googleMapsUrl: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    hours: Optional[Dict] = None
    priceLevel: Optional[int] = None

class ItineraryRequest(BaseModel):
    pois: List[POI]
    preferences: Dict = {}
    constraints: Dict = {}

class ItineraryResponse(BaseModel):
    status: str
    optimized_pois: List[POI]
    total_distance: float
    total_time: int
    route_order: List[str]