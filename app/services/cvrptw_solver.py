from typing import List, Dict, Tuple
from app.schemas.itinerary import POI

def calculate_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
    """Calculate distance between two coordinates (simplified)"""
    import math
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    
    # Haversine formula (simplified)
    R = 6371  # Earth's radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2) * math.sin(dlat/2) + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2) * math.sin(dlon/2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def optimize_route(pois: List[POI]) -> Tuple[List[str], float]:
    """Simple greedy nearest-neighbor route optimization"""
    if not pois or len(pois) == 0:
        return [], 0.0
    
    if len(pois) == 1:
        return [pois[0].id], 0.0
    
    unvisited = set(p.id for p in pois)
    current_id = pois[0].id
    route = [current_id]
    unvisited.remove(current_id)
    total_distance = 0.0
    
    poi_map = {p.id: p for p in pois}
    current = poi_map[current_id]
    
    while unvisited:
        nearest_id = min(
            unvisited,
            key=lambda pid: calculate_distance(
                (current.coordinates.lat, current.coordinates.lng),
                (poi_map[pid].coordinates.lat, poi_map[pid].coordinates.lng)
            ) if current.coordinates and poi_map[pid].coordinates else float('inf')
        )
        
        if current.coordinates and poi_map[nearest_id].coordinates:
            distance = calculate_distance(
                (current.coordinates.lat, current.coordinates.lng),
                (poi_map[nearest_id].coordinates.lat, poi_map[nearest_id].coordinates.lng)
            )
            total_distance += distance
        
        route.append(nearest_id)
        unvisited.remove(nearest_id)
        current = poi_map[nearest_id]
    
    return route, total_distance
