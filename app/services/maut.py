import os, json, math
from typing import Dict, Any, List
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# ----- Config -----
BUDGET_TARGET = {
    "tight": 1.0,
    "sensible": 2.0,
    "upscale": 3.0,
    "premium": 4.0,
    "any": 4.0,
}

BASE_WEIGHTS = {
    "interest": 0.3,
    "cost": 0.2,
    "popularity": 0.1,
    "child": 0.1,
    "dietary": 0.1,
    "pet": 0.1,
    "access": 0.1,
}

# ----- Utils -----
def norm_01(x, lo, hi):
    if x is None or hi == lo: return 0.0
    v = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, v))

def popularity_score(rating, reviews):
    r = 0.0 if rating is None else max(0.0, min(1.0, float(rating) / 5.0))
    if not reviews or reviews <= 0:
        return 0.5 * r
    rc = min(1.0, math.log10(1.0 + reviews) / 3.0)  # ~cap at 1k reviews
    return 0.7 * r + 0.3 * rc

def budget_alignment(price_level, budget_tier):
    if price_level is None: return 0.5
    target = BUDGET_TARGET.get(budget_tier, 4.0)
    dist = abs(float(price_level) - target)
    return max(0.0, 1.0 - (dist / 3.0))

def dietary_score(req, poi):
    """Score 1.0 if POI satisfies *any* of user's dietary restrictions."""
    prefs = set(req.get("dietary_restrictions") or [])
    if not prefs:
        return 0.5  # neutral if user has no dietary preference

    # Each POI flag
    halal = poi.get("halal_food", False)
    vegan = poi.get("vegan_options", False)
    vegetarian = poi.get("vegetarian_options", False)

    # Evaluate
    score = 0.0
    if "halal" in prefs and halal:
        score = max(score, 1.0)
    if "vegan" in prefs and vegan:
        score = max(score, 1.0)
    if "vegetarian" in prefs and (vegetarian or vegan):
        score = max(score, 1.0)
    return score

def any_accessible(p):
    return bool(
        p.get('wheelchair_accessible_entrance') or
        p.get('wheelchair_accessible_seating') or
        p.get('wheelchair_accessible_toilet')
    )

def derive_selected_themes(req: Dict[str, Any]) -> List[str]:
    # ensure 3 themes
    t = list(dict.fromkeys(req.get("interest_categories", [])))
    fallback = ["shopping","cultural_history","nature"]
    for f in fallback:
        if len(t) >= 3: break
        if f not in t:
            t.append(f)
    return t[:3]

def role_keep_counts(num_days: int) -> Dict[str, int]:
    d = max(1, int(num_days or 3))
    return {
        "attraction": min(10 * d, 60),
        "meal":       min(10 * d, 60),
        "accommodation": min(max(6, d), 30),
    }

def applicable_dims(req: Dict[str, Any], poi: Dict[str, Any]):
    dims = {"interest","cost","popularity"}
    if req.get("flags", {}).get("has_child"):
        dims.add("child")
    if req.get("flags", {}).get("has_pets"):
        dims.add("pet")
    if "halal" in (req.get("dietary_restrictions") or []):
        if "meal" in (poi.get("poi_roles") or []):
            dims.add("dietary")
    if req.get("flags", {}).get("wheelchair_accessible"):
        dims.add("access")
    return dims

def renorm_weights(dims):
    s = sum(BASE_WEIGHTS[d] for d in dims)
    return {d: (BASE_WEIGHTS[d] / s) for d in dims} if s > 0 else {k:0 for k in dims}

# ----- theme-category lookup for interest score -----
def get_theme_lookup(selected_themes: List[str]) -> Dict[str, set]:
    if not selected_themes:
        return {}
    rows = sb.table("theme_category_map") \
             .select("theme,category") \
             .in_("theme", selected_themes) \
             .execute().data
    m: Dict[str, set] = {}
    for r in rows:
        m.setdefault(r["category"], set()).add(r["theme"])
    return m

def interest_match_score(categories, selected_themes, theme_cats_lookup):
    if not categories: return 0.0
    sel = set(selected_themes)
    hit = 0
    for c in categories:
        mapped = theme_cats_lookup.get(c, set())
        if mapped & sel:
            hit += 1
    return hit / max(1, len(categories))

# ----- Fetch via latest RPC (quota-aware) -----
def fetch_candidates(req: Dict[str, Any], selected_themes: List[str]) -> List[Dict[str, Any]]:
    quotas = role_keep_counts(req.get("num_days", 3))
    params = {
        "p_destination": req["destination"],
        "p_themes": selected_themes,
        "p_quota_attraction": quotas["attraction"],
        "p_quota_meal": quotas["meal"],
        "p_quota_accommodation": quotas["accommodation"],

        "p_roles": ["attraction","meal","accommodation"],
        "p_min_rating": 2.0,
        "p_min_reviews": 10,
        "p_per_area_cap": req.get("per_area_cap", 10),

        "p_halal_only": bool(req.get("flags", {}).get("is_muslim", False)),
        "p_wheelchair_only": bool(req.get("flags", {}).get("wheelchair_accessible", False)),
        "p_excluded_themes": req.get("excluded_themes") or None,
        "p_exclude_nightlife": bool(req.get("flags", {}).get("exclude_nightlife", False)),

        "p_seed_lon": req.get("seed_lon"),
        "p_seed_lat": req.get("seed_lat"),
    }
    # NOTE: matches your SQL name exactly:
    rsp = sb.rpc("rpc_fetch_poi_candidates_quota", params).execute()
    return rsp.data or []

# ----- Scoring -----
def score_poi(req: Dict[str, Any], poi: Dict[str, Any], theme_lookup: Dict[str, set], selected_themes: List[str]) -> float:
    dims = applicable_dims(req, poi)
    W = renorm_weights(dims)

    s_interest = interest_match_score(poi.get("categories"), selected_themes, theme_lookup) if "interest" in W else 0
    s_cost     = budget_alignment(poi.get("price_level"), req.get("budget_tier")) if "cost" in W else 0
    s_pop      = popularity_score(poi.get("review_rating"), poi.get("review_count")) if "popularity" in W else 0
    s_child    = 1.0 if ("child" in W and poi.get("kids_friendly")) else (0 if "child" in W else 0)
    s_diet     = dietary_score(req, poi) if "dietary" in W else 0
    s_pet      = 1.0 if ("pet" in W and poi.get("pets_friendly")) else (0 if "pet" in W else 0)
    s_access   = 1.0 if ("access" in W and any_accessible(poi)) else (0 if "access" in W else 0)

    return float(
        W.get("interest",0)*s_interest +
        W.get("cost",0)*s_cost +
        W.get("popularity",0)*s_pop +
        W.get("child",0)*s_child +
        W.get("dietary",0)*s_diet +
        W.get("pet",0)*s_pet +
        W.get("access",0)*s_access
    )

def trim_by_role(scored: List[Dict[str, Any]], num_days: int) -> List[Dict[str, Any]]:
    # SQL already applied quotas, this is a safe guard if RPC returns >quota
    keep = role_keep_counts(num_days)
    out: List[Dict[str, Any]] = []
    for role, k in keep.items():
        bunch = [r for r in scored if role in (r.get("poi_roles") or [])]
        bunch.sort(key=lambda x: x["_score"], reverse=True)
        out.extend(bunch[:k])
    return out

# ----- Orchestrator -----
def run_pipeline(request_path="tests/maut_test.json") -> Dict[str, Any]:
    with open(request_path, "r", encoding="utf-8") as f:
        req = json.load(f)

    selected_themes = derive_selected_themes(req)
    theme_lookup = get_theme_lookup(selected_themes)

    rows = fetch_candidates(req, selected_themes)

    scored = []
    for p in rows:
        p["_score"] = score_poi(req, p, theme_lookup, selected_themes)
        scored.append(p)

    trimmed = trim_by_role(scored, req.get("num_days", 3))
    trimmed.sort(key=lambda x: x["_score"], reverse=True)

    return {
        "selected_themes": selected_themes,
        "count_in": len(rows),
        "count_out": len(trimmed),
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "roles": r.get("poi_roles"),
                "categories": r.get("categories"),
                "rating": r.get("review_rating"),
                "reviews": r.get("review_count"),
                "price_level": r.get("price_level"),
                "flags": {
                    "kids": r.get("kids_friendly"),
                    "pet": r.get("pets_friendly"),
                    "halal": r.get("halal_food"),
                    "access": any_accessible(r)
                },
                "score": round(r["_score"], 4)
            } for r in trimmed
        ]
    }
