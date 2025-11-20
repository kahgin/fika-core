from __future__ import annotations

import os
import math
from dotenv import load_dotenv
from supabase import create_client
from typing import Any, Dict, List, Optional, Set, TypedDict
from app.schemas.itinerary import POI, Coordinates, ItineraryResponse

# Supabase client

load_dotenv()
_sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# Config

BUDGET_TARGET = {"tight": 1.0, "sensible": 2.0, "upscale": 3.0, "luxury": 4.0}

BASE_WEIGHTS = {
    "interest": 0.3,
    "cost": 0.2,
    "popularity": 0.1,
    "child": 0.1,
    "dietary": 0.1,
    "pet": 0.1,
    "access": 0.1,
}


# Internal DTO
class Row(TypedDict, total=False):
    id: str
    name: str
    poi_roles: Optional[List[str]]
    categories: Optional[List[str]]
    themes: Optional[List[str]]
    open_hours: Optional[Dict[str, Any]]
    review_count: Optional[int]
    review_rating: Optional[float]
    latitude: float
    longitude: float
    price_level: Optional[float]
    images: Optional[List[str]]
    kids_friendly: Optional[bool]
    pets_friendly: Optional[bool]
    wheelchair_accessible_entrance: Optional[bool]
    wheelchair_accessible_seating: Optional[bool]
    wheelchair_accessible_toilet: Optional[bool]
    halal_food: Optional[bool]
    vegan_options: Optional[bool]
    vegetarian_options: Optional[bool]
    role_pick: Optional[str]
    area_name: Optional[str]
    distance_m: Optional[float]
    _score: float
    _role: Optional[str]


# Helpers


def popularity_score(rating: Optional[float], reviews: Optional[int]) -> float:
    r = 0.0 if rating is None else max(0.0, min(1.0, float(rating) / 5.0))
    if not reviews or reviews <= 0:
        return 0.5 * r
    rc = min(1.0, math.log10(1.0 + reviews) / 3.0)
    return 0.7 * r + 0.3 * rc


def budget_alignment(price_level: Optional[float], budget_tier: str) -> float:
    if price_level is None:
        return 1.0
    target = BUDGET_TARGET.get(budget_tier, 4.0)
    dist = abs(float(price_level) - target)
    return max(0.0, 1.0 - (dist / 3.0))


def any_accessible(p: Row) -> bool:
    return bool(
        p.get("wheelchair_accessible_entrance")
        or p.get("wheelchair_accessible_seating")
        or p.get("wheelchair_accessible_toilet")
    )


def derive_selected_themes(req: Dict[str, Any]) -> List[str]:
    t = list(dict.fromkeys(req.get("interest_themes", [])))
    fallback = ["shopping", "cultural_history", "nature"]
    for f in fallback:
        if len(t) >= 3:
            break
        if f not in t:
            t.append(f)
    return t[:3]


def role_keep_counts(num_days: int) -> Dict[str, int]:
    d = max(1, int(num_days or 7))
    return {
        "attraction": min(12 * d, 300),
        "meal": min(5 * d, 50),
        "accommodation": min(d + 5, 15),  # At least d+5 to ensure options
    }


def applicable_dims(req: Dict[str, Any], poi_roles: List[str]) -> Set[str]:
    dims: Set[str] = {"interest", "cost", "popularity"}
    flags = req.get("flags", {})
    if flags.get("has_child"):
        dims.add("child")
    if flags.get("has_pets"):
        dims.add("pet")
    if "halal" in (req.get("dietary_restrictions") or []) and (
        "meal" in (poi_roles or [])
    ):
        dims.add("dietary")
    if flags.get("wheelchair_accessible"):
        dims.add("access")
    return dims


def renorm_weights(dims: Set[str]) -> Dict[str, float]:
    s = sum(BASE_WEIGHTS[d] for d in dims)
    return {d: (BASE_WEIGHTS[d] / s) for d in dims} if s > 0 else {k: 0.0 for k in dims}


def interest_match_score(
    poi_themes: Optional[List[str]], selected_themes: List[str]
) -> float:
    """Score POI by directly matching its themes with user-selected themes."""
    if not poi_themes or not selected_themes:
        return 0.0

    poi_theme_set = set(poi_themes)
    selected_theme_set = set(selected_themes)

    # Count how many user themes match POI themes
    matches = len(poi_theme_set & selected_theme_set)

    # Normalize by number of selected themes
    return matches / len(selected_themes)


# Supabase RPC


def fetch_candidates(req: Dict[str, Any], selected_themes: List[str]) -> List[Row]:
    quotas = role_keep_counts(req.get("num_days", 3))
    params = {
        "p_destination": req["destination"],
        "p_themes": selected_themes,
        "p_quota_attraction": quotas["attraction"],
        "p_quota_meal": quotas["meal"],
        "p_quota_accommodation": quotas["accommodation"],
        "p_roles": ["attraction", "meal", "accommodation"],
        "p_min_rating": 2.0,
        "p_min_reviews": 10,
        "p_halal_only": bool(req.get("flags", {}).get("is_muslim", False)),
        "p_wheelchair_only": bool(
            req.get("flags", {}).get("wheelchair_accessible", False)
        ),
        "p_excluded_themes": req.get("excluded_themes") or None,
        "p_exclude_nightlife": bool(
            req.get("flags", {}).get("exclude_nightlife", False)
        ),
        "p_seed_lon": req.get("seed_lon"),
        "p_seed_lat": req.get("seed_lat"),
    }
    rsp = _sb.rpc("rpc_fetch_poi_candidates_quota", params).execute()
    return list(rsp.data or [])


# Scoring


def dietary_score(req: Dict[str, Any], poi: Row) -> float:
    prefs = set(req.get("dietary_restrictions") or [])
    if not prefs:
        return 0.5
    halal = bool(poi.get("halal_food"))
    vegan = bool(poi.get("vegan_options"))
    vegetarian = bool(poi.get("vegetarian_options"))
    hit = (
        ("halal" in prefs and halal)
        or ("vegan" in prefs and vegan)
        or ("vegetarian" in prefs and (vegetarian or vegan))
    )
    return 1.0 if hit else 0.0


def score_row(req: Dict[str, Any], row: Row, selected_themes: List[str]) -> float:
    roles = row.get("poi_roles") or []
    dims = applicable_dims(req, roles)
    W = renorm_weights(dims)

    # Theme matching only for attractions, not for meals or accommodations
    is_attraction = (
        "attraction" in roles and "meal" not in roles and "accommodation" not in roles
    )
    s_interest = (
        interest_match_score(row.get("themes"), selected_themes)
        if ("interest" in W and is_attraction)
        else 0.0
    )

    s_cost = (
        budget_alignment(row.get("price_level"), req.get("budget_tier"))
        if "cost" in W
        else 0.0
    )
    s_pop = (
        popularity_score(row.get("review_rating"), row.get("review_count"))
        if "popularity" in W
        else 0.0
    )
    s_child = (
        1.0
        if ("child" in W and row.get("kids_friendly"))
        else (0.0 if "child" in W else 0.0)
    )
    s_diet = dietary_score(req, row) if "dietary" in W else 0.0
    s_pet = (
        1.0
        if ("pet" in W and row.get("pets_friendly"))
        else (0.0 if "pet" in W else 0.0)
    )
    s_access = (
        1.0
        if ("access" in W and any_accessible(row))
        else (0.0 if "access" in W else 0.0)
    )

    return float(
        W.get("interest", 0) * s_interest
        + W.get("cost", 0) * s_cost
        + W.get("popularity", 0) * s_pop
        + W.get("child", 0) * s_child
        + W.get("dietary", 0) * s_diet
        + W.get("pet", 0) * s_pet
        + W.get("access", 0) * s_access
    )


def trim_by_role(
    scored: List[Row], num_days: int, selected_themes: List[str]
) -> Dict[str, List[Row]]:
    """
    Trim scored POIs by role quotas and return structured by role.
    Ensures minimum POIs per role AND theme balance for attractions.

    Returns:
        {
            "attraction": [Row, ...],
            "meal": [Row, ...],
            "accommodation": [Row, ...]
        }
    """
    keep = role_keep_counts(num_days)

    # Group by role - POIs can appear in multiple role groups
    by_role: Dict[str, List[Row]] = {"attraction": [], "meal": [], "accommodation": []}

    for r in scored:
        roles = r.get("poi_roles") or []

        # Meals: any POI that has a meal role
        if "meal" in roles:
            by_role["meal"].append(r)

        # Pure accommodation only: no attraction/meal role
        if (
            "accommodation" in roles
            and "attraction" not in roles
            and "meal" not in roles
        ):
            by_role["accommodation"].append(r)

        # Attractions: anything marked as attraction (even if also meal/accommodation)
        if "attraction" in roles:
            by_role["attraction"].append(r)

        # If no explicit roles at all, treat as attraction
        if not roles or (
            not any(role in roles for role in ["meal", "accommodation", "attraction"])
        ):
            by_role["attraction"].append(r)

    # Sort each role by score
    for role in by_role:
        by_role[role].sort(key=lambda x: x["_score"], reverse=True)

    # Trim to quotas - process in priority order to avoid duplicates
    result: Dict[str, List[Row]] = {}
    seen: Set[str] = set()

    # Priority order: accommodation > meal > attraction (with theme balance)
    for role in ["accommodation", "meal"]:
        quota = keep[role]
        result[role] = []
        picked = 0
        for r in by_role[role]:
            rid = r["id"]
            if rid in seen:
                continue
            result[role].append(r)
            seen.add(rid)
            picked += 1
            if picked >= quota:
                break

    # Special handling for attractions - ensure theme balance
    result["attraction"] = []
    if selected_themes and by_role["attraction"]:
        # Calculate target per theme
        quota = keep["attraction"]
        target_per_theme = quota // len(selected_themes)
        remainder = quota % len(selected_themes)

        # Group attractions by theme
        by_theme: Dict[str, List[Row]] = {theme: [] for theme in selected_themes}
        no_theme: List[Row] = []

        for r in by_role["attraction"]:
            if r["id"] in seen:
                continue
            themes = r.get("themes", [])
            matched = False
            for theme in selected_themes:
                if theme in themes:
                    by_theme[theme].append(r)
                    matched = True
                    break
            if not matched:
                no_theme.append(r)

        # Pick from each theme
        picked = 0
        for theme_idx, theme in enumerate(selected_themes):
            # Add 1 extra to first themes if there's remainder
            theme_quota = target_per_theme + (1 if theme_idx < remainder else 0)
            theme_pois = by_theme[theme]

            for r in theme_pois[:theme_quota]:
                if r["id"] not in seen:
                    result["attraction"].append(r)
                    seen.add(r["id"])
                    picked += 1

        # Fill remaining quota with highest scored POIs (including no_theme)
        if picked < quota:
            remaining = [r for r in by_role["attraction"] if r["id"] not in seen]
            for r in remaining[: (quota - picked)]:
                result["attraction"].append(r)
                seen.add(r["id"])
    else:
        # No theme balancing needed
        quota = keep["attraction"]
        picked = 0
        for r in by_role["attraction"]:
            rid = r["id"]
            if rid in seen:
                continue
            result["attraction"].append(r)
            seen.add(rid)
            picked += 1
            if picked >= quota:
                break

    return result


# Mapping to API POI


def to_poi(row: Row) -> POI:
    """Convert internal Row to POI schema with all fields for CVRPTW."""
    return POI(
        id=row["id"],
        name=row["name"],
        poi_roles=row.get("poi_roles") or [],
        category=(row.get("categories") or [None])[0],
        categories=row.get("categories") or [],
        themes=row.get("themes", []),
        rating=row.get("review_rating"),
        reviewCount=row.get("review_count"),
        images=row.get("images") or [],
        coordinates=Coordinates(
            lat=float(row["latitude"]), lng=float(row["longitude"])
        ),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        openHours=row.get("open_hours"),
        priceLevel=(
            int(row["price_level"]) if row.get("price_level") is not None else None
        ),
    )


# Orchestrator


def run_pipeline(payload: Dict[str, Any], *, as_model: bool = False):
    """
    Run MAUT pipeline to score and select POIs.

    Args:
        payload: Internal MAUT request (already transformed from frontend)
        as_model: If True, return Pydantic model; else return dict

    Returns:
        ItineraryResponse with scored POIs structured by role
    """
    # 1) Derive selected themes (3 themes with fallback)
    selected_themes = derive_selected_themes(payload)

    # 2) Fetch POI candidates from Supabase RPC
    rows: List[Row] = fetch_candidates(payload, selected_themes)

    # 3) Score each POI using MAUT algorithm
    scored: List[Row] = []
    for r in rows:
        r["_score"] = score_row(payload, r, selected_themes)
        scored.append(r)

    # 4) Trim by role quotas - returns dict by role with theme balance
    trimmed_by_role = trim_by_role(scored, payload.get("num_days", 3), selected_themes)

    # 5) Flatten and sort all POIs by score for places list
    all_trimmed: List[Row] = []
    for role_pois in trimmed_by_role.values():
        all_trimmed.extend(role_pois)
    all_trimmed.sort(key=lambda x: x["_score"], reverse=True)

    # 6) Map internal Row format to API POI format
    pois = [to_poi(r) for r in all_trimmed]

    # 7) Also create role-separated POI lists for CVRPTW
    pois_by_role = {
        role: [to_poi(r) for r in rows_list]
        for role, rows_list in trimmed_by_role.items()
    }

    # 7.1) Select default hotel from accommodations (highest scored)
    accom_rows = trimmed_by_role.get("accommodation", [])
    selected_hotel_poi: Optional[POI] = None
    if accom_rows:
        best_hotel_row = accom_rows[0]
        selected_hotel_poi = to_poi(best_hotel_row)

    # 8) Build response (CVRPTW/ACO will compute route_order, total_distance, total_time)
    resp = ItineraryResponse(
        status="ok",
        places=pois,
        total_distance=0.0,
        total_time=0,
        route_order=[],
        meta={
            "selected_themes": selected_themes,
            "count_in": len(rows),
            "count_out": len(all_trimmed),
            "by_role": {
                "attraction": len(trimmed_by_role["attraction"]),
                "meal": len(trimmed_by_role["meal"]),
                "accommodation": len(trimmed_by_role["accommodation"]),
            },
            "pois_by_role": {
                role: [
                    p.model_dump() if hasattr(p, "model_dump") else p for p in pois_list
                ]
                for role, pois_list in pois_by_role.items()
            },
            "num_days": payload.get("num_days"),
            "dates": payload.get("dates"),
            "selected_hotel": (
                selected_hotel_poi.model_dump() if selected_hotel_poi else None
            ),
        },
    )
    return resp if as_model else resp.model_dump()
