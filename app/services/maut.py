from __future__ import annotations

import os
import math
from supabase import create_client
from typing import Any, Dict, List, Optional, Set, TypedDict
from app.schemas.itinerary import POI, Coordinates, ItineraryResponse
from app.utils.logger import get_logger

# Supabase client

_sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
logger = get_logger(__name__)

# Config

BASE_WEIGHTS = {"interest": 0.3, "popularity": 0.1, "child": 0.15, "dietary": 0.15, "pet": 0.15, "access": 0.15 } # "cost": 0.2
# BUDGET_TARGET = {"tight": 1.0, "sensible": 2.0, "upscale": 3.0, "luxury": 4.0}

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


# def budget_alignment(price_level: Optional[float], budget_tier: str) -> float:
#     if price_level is None:
#         return 1.0
#     target = BUDGET_TARGET.get(budget_tier, 4.0)
#     dist = abs(float(price_level) - target)
#     return max(0.0, 1.0 - (dist / 3.0))


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
        "meal": min(8 * d, 50),
        "accommodation": min(d + 5, 15),
    }


def applicable_dims(req: Dict[str, Any], poi_roles: List[str]) -> Set[str]:
    dims: Set[str] = {"interest", "popularity"}  # "cost"
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

def _rpc_fetch(
    req: Dict[str, Any],
    selected_themes: List[str],
    *,
    include_images: bool = True,
    kids_only: bool = False,
    pets_only: bool = False,
    halal_only: bool = False,
    vegetarian_only: bool = False,
    vegan_only: bool = False,
    wheelchair_only: bool = False,
) -> List[Row]:
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
        "p_halal_only": bool(halal_only),
        "p_vegetarian_only": bool(vegetarian_only),
        "p_vegan_only": bool(vegan_only),
        "p_wheelchair_only": bool(wheelchair_only),
        "p_kids_friendly_only": bool(kids_only),
        "p_pets_friendly_only": bool(pets_only),
        "p_include_images": bool(include_images),
        "p_excluded_themes": req.get("excluded_themes") or None,
        "p_seed_lon": req.get("seed_lon"),
        "p_seed_lat": req.get("seed_lat"),
    }
    rsp = _sb.rpc("rpc_fetch_poi_candidates_quota", params).execute()
    return list(rsp.data or [])


def fetch_candidates(req: Dict[str, Any], selected_themes: List[str]) -> List[Row]:
    """Initial fetch with strict constraints derived from request."""
    flags = req.get("flags", {}) or {}
    dietary = set(req.get("dietary_restrictions") or [])
    return _rpc_fetch(
        req,
        selected_themes,
        include_images=True,
        kids_only=bool(flags.get("has_child")),
        pets_only=bool(flags.get("has_pets")),
        halal_only=bool("halal" in dietary or flags.get("is_muslim")),
        vegetarian_only=bool("vegetarian" in dietary),
        vegan_only=bool("vegan" in dietary),
        wheelchair_only=bool(flags.get("wheelchair_accessible")),
    )

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
    # s_cost = (
    #     budget_alignment(row.get("price_level"), req.get("budget_tier"))
    #     if "cost" in W
    #     else 0.0
    # )
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
        # + W.get("cost", 0) * s_cost
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
        primary = roles[0] if roles else (r.get("role_pick") or "attraction")
        if primary == "meal":
            by_role["meal"].append(r)
        elif primary == "accommodation":
            by_role["accommodation"].append(r)
        else:
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
    """Convert internal Row to POI schema with all fields."""
    roles = row.get("poi_roles") or []
    if not roles and row.get("role_pick"):
        roles = [str(row.get("role_pick"))]
    return POI(
        id=row["id"],
        name=row["name"],
        poi_roles=roles,
        category=(row.get("categories") or [None])[0],
        categories=row.get("categories") or [],
        themes=row.get("themes", []),
        rating=row.get("review_rating"),
        reviewCount=row.get("review_count"),
        images=row.get("images") or [],
        coordinates=Coordinates(
            lat=float(row["latitude"]), lng=float(row["longitude"])
        ),
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

    # 2) Fetch POI candidates with potential fallback relaxation
    flags = payload.get("flags", {}) or {}
    dietary = set(payload.get("dietary_restrictions") or [])

    quotas = role_keep_counts(payload.get("num_days", 3))

    # Initial strict toggles
    t_kids = bool(flags.get("has_child"))
    t_pets = bool(flags.get("has_pets"))
    t_halal = bool("halal" in dietary or flags.get("is_muslim"))
    t_veg = bool("vegetarian" in dietary)
    t_vegan = bool("vegan" in dietary)
    t_access = bool(flags.get("wheelchair_accessible"))

    constraints_relaxed: List[str] = []

    def _run_round(
        kids_only: bool, pets_only: bool
    ) -> tuple[List[Row], Dict[str, List[Row]]]:
        rows_i: List[Row] = _rpc_fetch(
            payload,
            selected_themes,
            include_images=True,
            kids_only=kids_only,
            pets_only=pets_only,
            halal_only=t_halal,
            vegetarian_only=t_veg,
            vegan_only=t_vegan,
            wheelchair_only=t_access,
        )
        for rr in rows_i:
            rr["_score"] = score_row(payload, rr, selected_themes)
        trimmed_i = trim_by_role(rows_i, payload.get("num_days", 3), selected_themes)
        return rows_i, trimmed_i

    # Round 1: strict
    rows, trimmed_by_role = _run_round(t_kids, t_pets)

    def _meets_quota(tb: Dict[str, List[Row]]) -> bool:
        return (
            len(tb.get("attraction", [])) >= quotas["attraction"]
            and len(tb.get("meal", [])) >= quotas["meal"]
            and len(tb.get("accommodation", [])) >= quotas["accommodation"]
        )

    # Relaxation order: pets -> kids (dietary & wheelchair stay as set)
    if not _meets_quota(trimmed_by_role):
        if t_pets:
            logger.info(
                {
                    "event": "maut.relaxation",
                    "action": "relax_pets_friendly_only",
                    "reason": "quota_underfilled",
                }
            )
            constraints_relaxed.append("pets_friendly_only")
            rows, trimmed_by_role = _run_round(t_kids, False)

    if not _meets_quota(trimmed_by_role):
        if t_kids:
            logger.info(
                {
                    "event": "maut.relaxation",
                    "action": "relax_kids_friendly_only",
                    "reason": "quota_underfilled",
                }
            )
            constraints_relaxed.append("kids_friendly_only")
            rows, trimmed_by_role = _run_round(False, False)

    # 3) Flatten and sort all POIs by score for places list
    all_trimmed: List[Row] = []
    for role_pois in trimmed_by_role.values():
        all_trimmed.extend(role_pois)
    all_trimmed.sort(key=lambda x: x["_score"], reverse=True)

    # 4) Map internal Row format to API POI format
    pois = [to_poi(r) for r in all_trimmed]

    # 5) Also create role-separated POI lists for CVRPTW
    pois_by_role = {
        role: [to_poi(r) for r in rows_list]
        for role, rows_list in trimmed_by_role.items()
    }

    # 6) Select default hotel from accommodations (highest scored)
    accom_rows = trimmed_by_role.get("accommodation", [])
    selected_hotel_poi: Optional[POI] = None
    if accom_rows:
        best_hotel_row = accom_rows[0]
        selected_hotel_poi = to_poi(best_hotel_row)

    # 7) Build response
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
            "constraints_relaxed": constraints_relaxed,
        },
    )
    return resp if as_model else resp.model_dump()
