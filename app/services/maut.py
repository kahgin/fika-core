from typing import List, Dict
from app.schemas.itinerary import POI
import logging

logger = logging.getLogger(__name__)

def score_poi(poi: POI, preferences: Dict) -> float:
    """Score a POI based on user preferences"""
    score = 0.0
    
    # Rating weight (0-30 points)
    score += (poi.rating / 5.0) * 30
    
    # Category preference (0-40 points)
    category_scores = preferences.get('categories', {})
    category_score = category_scores.get(poi.category.lower(), 20)
    score += category_score
    
    # Price preference (0-30 points)
    if poi.priceLevel:
        max_price = preferences.get('max_price_level', 4)
        if poi.priceLevel <= max_price:
            score += (max_price - poi.priceLevel + 1) * 7.5
    
    return score

def score_all_pois(pois: List[POI], preferences: Dict) -> Dict[str, float]:
    """Score all POIs and return as dict"""
    scores = {}
    for poi in pois:
        scores[poi.id] = score_poi(poi, preferences)
    return scores
