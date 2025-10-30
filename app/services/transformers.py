import json
from typing import List, Dict
from app.schemas.itinerary import POI, Coordinates

def extract_images(poi: Dict) -> List[str]:
    """Extract all images from POI data"""
    images = []
    
    if poi.get('images'):
        if isinstance(poi['images'], list):
            for img_data in poi['images']:
                img_url = None
                if isinstance(img_data, dict) and 'image' in img_data:
                    img_url = img_data['image']
                elif isinstance(img_data, str):
                    try:
                        img_json = json.loads(img_data)
                        if 'image' in img_json:
                            img_url = img_json['image']
                    except:
                        if img_data.startswith('http'):
                            img_url = img_data
                
                if img_url and img_url.startswith('http'):
                    images.append(img_url)
        elif isinstance(poi['images'], str):
            try:
                img_json = json.loads(poi['images'])
                if isinstance(img_json, list):
                    for img in img_json:
                        if isinstance(img, dict) and 'image' in img:
                            images.append(img['image'])
                        elif isinstance(img, str) and img.startswith('http'):
                            images.append(img)
                elif 'image' in img_json:
                    images.append(img_json['image'])
            except:
                if poi['images'].startswith('http'):
                    images.append(poi['images'])
    
    if not images and poi.get('image'):
        images.append(poi['image'])
    
    return images

def transform_supabase_poi(poi: Dict) -> POI:
    """Transform Supabase POI data to frontend format"""
    
    category = "Attraction"
    if poi.get('categories'):
        if isinstance(poi['categories'], list) and len(poi['categories']) > 0:
            category = poi['categories'][0]
        elif isinstance(poi['categories'], str):
            category = poi['categories'].split(',')[0].strip()
    
    images = extract_images(poi)
    
    location = "Singapore"
    if poi.get('complete_address'):
        if isinstance(poi['complete_address'], dict):
            city = poi['complete_address'].get('city', '')
            state = poi['complete_address'].get('state', '')
            location = f"{city}, {state}" if city else state
        location = location.strip(', ') or "Singapore"
    elif poi.get('address'):
        parts = poi['address'].split(',')
        if len(parts) > 1:
            location = parts[-1].strip()
    
    coordinates = None
    if poi.get('latitude') and poi.get('longitude'):
        coordinates = Coordinates(
            lat=float(poi['latitude']),
            lng=float(poi['longitude'])
        )
    
    return POI(
        id=str(poi['id']),
        name=poi['name'],
        category=category,
        rating=float(poi.get('review_rating', 4.0)),
        reviewCount=int(poi.get('review_count', 0)),
        location=location,
        images=images,
        description=poi.get('descriptions', ''),
        coordinates=coordinates,
        website=poi.get('website'),
        googleMapsUrl=poi.get('google_map_link'),
        address=poi.get('address'),
        phone=poi.get('phone'),
        hours=poi.get('open_hours'),
        priceLevel=int(poi.get('price_level')) if poi.get('price_level') else None
    )
