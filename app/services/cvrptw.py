from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from app.services.osrm import osrm_client

# Configuration


PACE_DAY_BUDGET_MIN = {
    "relaxed": 9 * 60,  # 09:00–18:00
    "balanced": 11 * 60,  # 09:00–20:00
    "packed": 13 * 60,  # 09:00–22:00
}

SERVICE_TIME = {
    "attraction": {"relaxed": 120, "balanced": 90, "packed": 60},
    "meal": {"relaxed": 75, "balanced": 60, "packed": 45},
    "accommodation": {"relaxed": 0, "balanced": 0, "packed": 0},
}

# Default time windows when open_hours is missing (minutes from midnight)
DEFAULT_ROLE_WINDOWS = {
    "attraction": (9 * 60, 19 * 60),  # 09:00–19:00
    "meal": (10 * 60, 22 * 60),  # 10:00–22:00
    "accommodation": (0, 24 * 60),  # 24h for stay
    "depot": (0, 24 * 60),  # hotel depot
}

# Global “good” meal windows (used also in ACS)
LUNCH_WIN = (12 * 60, 14 * 60)  # 12:00–14:00
DINNER_WIN = (18 * 60, 21 * 60)  # 18:00–21:00

# Extra windows for hard “meals around meal time” in OR-Tools version
BREAKFAST_WIN = (7 * 60, 10 * 60)  # 07:00–10:00

# How far around these windows we still allow meals (hard constraint)
MEAL_HARD_TOL = 90  # minutes; meal must start within ±90 min of some meal window

# Penalties. Values are in “minute-cost” units on top of travel+service time.
# These are deliberately large so that:
# - Consecutive meals almost never appear in optimal solutions.
# - Back-to-back same-theme transitions are strongly discouraged.
PENALTY_MEAL_TO_MEAL = 5000
PENALTY_SAME_THEME = 500
DROP_PENALTY_BASE = 2000  # Base penalty for dropping a POI (non-mandatory)


# Data Structures


@dataclass
class DaySpec:
    day_index: int
    date: dt.date
    start_min: int
    end_min: int
    depot_id: str


@dataclass
class Node:
    idx: int
    poi_id: str
    name: str
    role: str
    lat: float
    lon: float
    service: int
    themes: Optional[List[str]]
    windows_by_day: Dict[int, List[Tuple[int, int]]]
    is_mandatory: bool = False


# Helper Functions


def parse_time_range_label(label: str) -> Optional[Tuple[int, int]]:
    """Parse time range like '10 am-9 pm' to (600, 1260)."""
    s = label.strip()
    if "closed" in s.lower() or "open 24 hours" in s.lower():
        return None if "closed" in s.lower() else (0, 24 * 60)

    try:
        left, right = [x.strip() for x in s.split("-")]

        def to_min(x: str) -> int:
            x = x.lower().replace(" ", "")
            ampm = "am" if "am" in x else "pm"
            hhmm = x.replace("am", "").replace("pm", "")
            if ":" in hhmm:
                h, m = hhmm.split(":")
                h, m = int(h), int(m)
            else:
                h, m = int(hhmm), 0
            if ampm == "am":
                if h == 12:
                    h = 0
            else:
                if h != 12:
                    h += 12
            return h * 60 + m

        a, b = to_min(left), to_min(right)
        if b <= a:
            # Handle cases like 8am–2am (next day) by capping at midnight
            b = 24 * 60
        return (a, b)
    except Exception:
        return None


def weekday_name(d: dt.date) -> str:
    """Return weekday name like 'Monday'."""
    return d.strftime("%A")


def minutes(hhmm: str) -> int:
    """Convert 'HH:MM' to minutes from midnight."""
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def pick_theme(themes: List[str], selected_themes: List[str]) -> Optional[str]:
    """Pick first matching theme from POI themes."""
    cat_s = " ".join(themes or []).lower()
    for t in selected_themes:
        if t.replace("_", " ") in cat_s:
            return t
    return None


def day_span(pacing: str) -> Tuple[int, int]:
    """Return (start_min, end_min) for a day based on pacing."""
    horizon = PACE_DAY_BUDGET_MIN.get(pacing, PACE_DAY_BUDGET_MIN["balanced"])
    return (9 * 60, 9 * 60 + horizon)


def extract_windows_for_date(
    open_hours: Optional[Dict[str, List[str]]],
    date: dt.date,
    default_window: Tuple[int, int],
) -> List[Tuple[int, int]]:
    """Extract time windows for a specific date from openHours."""
    d_start, d_end = default_window
    if not open_hours:
        return [default_window]

    wn = weekday_name(date)
    raw = open_hours.get(wn)
    if not raw:
        return [default_window]

    out: List[Tuple[int, int]] = []
    closed_explicit = False
    for lab in raw:
        if "closed" in lab.lower():
            closed_explicit = True
            continue
        rng = parse_time_range_label(lab)
        if not rng:
            continue
        a, b = rng
        a1, b1 = max(a, d_start), min(b, d_end)
        if a1 <= b1:
            out.append((a1, b1))

    if out:
        return out
    if closed_explicit:
        return []
    return [default_window]


def _restrict_meal_windows(
    windows: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """
    Restrict meal POI windows to be close to breakfast/lunch/dinner windows.

    This makes "meal at meal time" a hard constraint in OR-Tools by shrinking
    the allowed time windows for meal nodes, similar to what ACS enforces
    dynamically in its simulation with soft/hard tolerances.
    """
    if not windows:
        return []

    meal_windows = [BREAKFAST_WIN, LUNCH_WIN, DINNER_WIN]
    allowed: List[Tuple[int, int]] = []

    for w_start, w_end in windows:
        for m_start, m_end in meal_windows:
            # Expand meal window by MEAL_HARD_TOL on both sides
            ms = m_start - MEAL_HARD_TOL
            me = m_end + MEAL_HARD_TOL
            a = max(w_start, ms)
            b = min(w_end, me)
            if a < b:
                allowed.append((a, b))

    if not allowed:
        return []

    # Merge overlapping intervals
    allowed.sort()
    merged: List[Tuple[int, int]] = []
    cur_start, cur_end = allowed[0]
    for a, b in allowed[1:]:
        if a <= cur_end:
            cur_end = max(cur_end, b)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = a, b
    merged.append((cur_start, cur_end))
    return merged


# Build Problem from MAUT Output


def build_problem(
    maut_output: dict,
    hotel: Dict[str, float],
    pacing: str = "balanced",
    selected_themes: Optional[List[str]] = None,
    mandatory: Optional[Dict[str, Dict]] = None,
) -> Tuple[List[DaySpec], List[Node], List[List[int]]]:
    """
    Convert MAUT output to CVRPTW problem.

    Returns:
        (day_specs, nodes, travel_matrix_minutes)
    """
    meta = maut_output.get("meta", {})
    dates = meta.get("dates") or {}

    # num_days can be in meta or at root level
    num_days = meta.get("num_days") or maut_output.get("num_days")
    if not num_days or num_days <= 0:
        num_days = 3

    # Parse dates
    if dates.get("type") == "specific" and dates.get("startDate"):
        start = dt.date.fromisoformat(dates["startDate"])
        if dates.get("endDate"):
            end = dt.date.fromisoformat(dates["endDate"])
            num_days = (end - start).days + 1
    else:
        start = dt.date.today()

    # Day specs based on pacing
    day_specs: List[DaySpec] = []
    d_start, d_end = day_span(pacing)
    for k in range(num_days):
        day_specs.append(
            DaySpec(
                day_index=k,
                date=start + dt.timedelta(days=k),
                start_min=d_start,
                end_min=d_end,
                depot_id=hotel["id"],
            )
        )

    # Build nodes
    nodes: List[Node] = []
    idx = 0

    # Depot node
    depot = Node(
        idx=idx,
        poi_id=hotel["id"],
        name=hotel["name"],
        role="depot",
        lat=float(hotel["lat"]),
        lon=float(hotel["lon"]),
        service=0,
        themes=None,
        windows_by_day={d.day_index: [(d.start_min, d.end_min)] for d in day_specs},
    )
    nodes.append(depot)
    idx += 1

    # POI nodes
    sel_themes = maut_output.get("meta", {}).get("selected_themes", []) or []
    pois_by_role = meta.get("pois_by_role", {})

    if pois_by_role:
        # Structured by role
        for role in ["meal", "attraction", "accommodation"]:
            role_pois = pois_by_role.get(role, [])
            if role == "accommodation":
                # hotel depot already covers stay
                continue

            for poi in role_pois:
                for day_idx in range(num_days):
                    poi_copy = poi.copy()
                    poi_copy["id"] = f"{poi['id']}_day{day_idx}"
                    poi_copy["_day_specific"] = day_idx
                    _add_poi_node(
                        poi_copy,
                        role,
                        nodes,
                        idx,
                        day_specs,
                        pacing,
                        sel_themes,
                        mandatory,
                    )
                    idx += 1
    else:
        # Flat places list
        places = maut_output.get("places", [])
        for poi in places:
            roles = poi.get("poi_roles", [])
            if "meal" in roles:
                role = "meal"
            elif "accommodation" in roles:
                role = "accommodation"
            else:
                role = "attraction"

            for day_idx in range(num_days):
                poi_copy = poi.copy()
                poi_copy["id"] = f"{poi['id']}_day{day_idx}"
                poi_copy["_day_specific"] = day_idx
                _add_poi_node(
                    poi_copy,
                    role,
                    nodes,
                    idx,
                    day_specs,
                    pacing,
                    sel_themes,
                    mandatory,
                )
                idx += 1

    # Travel matrix in minutes from OSRM (same for OR-Tools and ACS)
    coords = [(n.lat, n.lon) for n in nodes]
    travel = osrm_client.matrix_minutes(coords)

    return day_specs, nodes, travel


def _add_poi_node(
    poi: Dict,
    role: str,
    nodes: List[Node],
    idx: int,
    day_specs: List[DaySpec],
    pacing: str,
    sel_themes: List[str],
    mandatory: Optional[Dict[str, Dict]],
) -> None:
    """Helper to add a POI node to the nodes list."""
    service = SERVICE_TIME[role][pacing]
    theme = pick_theme(poi.get("themes", []), sel_themes)

    # Extract coordinates
    coords = poi.get("coordinates")
    if coords:
        lat = coords.get("lat")
        lon = coords.get("lng")
    else:
        lat = poi.get("latitude")
        lon = poi.get("longitude")

    if lat is None or lon is None:
        return

    wbd: Dict[int, List[Tuple[int, int]]] = {}
    open_hours = poi.get("openHours")
    day_specific = poi.get("_day_specific")
    role_default = DEFAULT_ROLE_WINDOWS.get(role, (9 * 60, 21 * 60))

    if day_specific is not None:
        # Only available on one specific day
        d = day_specs[day_specific]
        day_start = max(d.start_min, role_default[0])
        day_end = min(d.end_min, role_default[1])
        day_default = (day_start, day_end)

        windows = extract_windows_for_date(open_hours, d.date, day_default)
        if role == "meal":
            windows = _restrict_meal_windows(windows)
        if not windows:
            return
        wbd[day_specific] = windows
    else:
        # Available on multiple days; derive windows per day
        for d in day_specs:
            day_start = max(d.start_min, role_default[0])
            day_end = min(d.end_min, role_default[1])
            day_default = (day_start, day_end)

            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if role == "meal":
                windows = _restrict_meal_windows(windows)
            if windows:
                wbd[d.day_index] = windows

        if not wbd:
            # Closed or unusable on all days
            return

    # Mandatory override: base POI ID and day-specific constraint window
    base_id = poi["id"].rsplit("_day", 1)[0]
    is_mand = False

    if mandatory and base_id in mandatory:
        md_spec = mandatory[base_id]
        dk = int(md_spec["day"]) - 1  # API is 1-based, internal 0-based
        if day_specific is None or day_specific == dk:
            a = minutes(md_spec["window"][0])
            b = minutes(md_spec["window"][1])
            wbd = {dk: [(a, b)]}
            is_mand = True

    nodes.append(
        Node(
            idx=idx,
            poi_id=poi["id"],
            name=poi["name"],
            role=role,
            themes=poi.get("themes", []),
            lat=float(lat),
            lon=float(lon),
            service=service,
            windows_by_day=wbd,
            is_mandatory=is_mand,
        )
    )


# OR-Tools Solver


def solve_cvrptw(
    day_specs: List[DaySpec],
    nodes: List[Node],
    travel: List[List[int]],
    meals_required: int = 2,
    time_limit_sec: int = 15,
) -> dict:
    """
    Solve CVRPTW using OR-Tools.

    This version:
    - Uses OSRM-based travel times (minutes).
    - Enforces opening hours and day pacing via time windows.
    - Enforces min/max meals per day via a Meals dimension (up to 3).
    - Strongly penalizes consecutive meals and repeated themes (soft, but large).
    - Ensures each base POI is visited at most once (disjunction over _dayX copies).
    - Applies huge penalty for skipping mandatory POIs.
    """
    if not day_specs:
        return {"days": [], "note": "No days specified"}
    if len(nodes) <= 1:
        return {"days": [], "note": "No POIs available"}

    N = len(nodes)
    V = len(day_specs)

    manager = pywrapcp.RoutingIndexManager(N, V, 0)
    routing = pywrapcp.RoutingModel(manager)

    # Transit callback with penalties similar to ACS rules
    def transit_cb(from_index, to_index):
        i, j = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        base = travel[i][j] + nodes[i].service
        bonus = 0

        # Strong cost for meal → meal (approximate "no consecutive meals")
        if nodes[i].role == "meal" and nodes[j].role == "meal":
            bonus += PENALTY_MEAL_TO_MEAL

        # Penalize same-theme transitions (approximate "no three similar in a row")
        if (
            nodes[i].themes
            and nodes[j].themes
            and nodes[i].themes[0] == nodes[j].themes[0]
        ):
            bonus += PENALTY_SAME_THEME

        return base + bonus

    t_idx = routing.RegisterTransitCallback(transit_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(t_idx)

    # Time dimension
    routing.AddDimension(
        t_idx,
        120,  # waiting/slack
        max(d.end_min for d in day_specs),
        False,
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Depot time windows
    for v, d in enumerate(day_specs):
        time_dim.CumulVar(routing.Start(v)).SetRange(d.start_min, d.end_min)
        time_dim.CumulVar(routing.End(v)).SetRange(d.start_min, d.end_min)

    # Node time windows and vehicle assignment
    for ni, n in enumerate(nodes):
        if n.role == "depot":
            continue

        available_days = list(n.windows_by_day.keys())

        if len(available_days) == 1:
            # Day-specific POI
            day_v = available_days[0]
            routing.SetAllowedVehiclesForIndex([day_v], manager.NodeToIndex(ni))
            a_min, b_max = n.windows_by_day[day_v][0]
            time_dim.CumulVar(manager.NodeToIndex(ni)).SetRange(a_min, b_max)
        else:
            # Multi-day POI: time windows per vehicle
            for day_v in available_days:
                if n.windows_by_day[day_v]:
                    a_min, b_max = n.windows_by_day[day_v][0]
                    time_dim.CumulVar(manager.NodeToIndex(ni)).SetRange(a_min, b_max)

    # Disjunctions (visit at most once) over base POI IDs
    by_poi: Dict[str, List[int]] = {}
    for i, n in enumerate(nodes):
        if n.role != "depot":
            base_id = n.poi_id.rsplit("_day", 1)[0]
            by_poi.setdefault(base_id, []).append(i)

    for poi_id, idxs in by_poi.items():
        any_mand = any(nodes[i].is_mandatory for i in idxs)
        penalty = 10_000_000 if any_mand else DROP_PENALTY_BASE
        routing.AddDisjunction(
            [manager.NodeToIndex(i) for i in idxs],
            penalty,
            1,  # at most 1 visit among copies
        )

    # Meals dimension (min/max meals per day, cap 3)
    def meal_cb(from_index, to_index):
        j = manager.IndexToNode(to_index)
        return 1 if nodes[j].role == "meal" else 0

    meal_idx = routing.RegisterTransitCallback(meal_cb)
    routing.AddDimension(
        meal_idx,
        0,
        3,  # max 3 meals per day
        True,
        "Meals",
    )
    meal_dim = routing.GetDimensionOrDie("Meals")

    if meals_required > 0:
        for v in range(V):
            available_meals = sum(
                1
                for n in nodes
                if n.role == "meal"
                and (len(n.windows_by_day) > 1 or v in n.windows_by_day)
            )
            req_min = min(meals_required, available_meals)
            req_max = min(3, available_meals)
            if req_min > 0:
                meal_dim.CumulVar(routing.End(v)).SetRange(req_min, req_max)

    # Search parameters
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(time_limit_sec)
    params.log_search = False
    routing.SetFixedCostOfAllVehicles(0)

    solution = routing.SolveWithParameters(params)

    result = {"days": []}
    if not solution:
        return {"days": [], "note": "No feasible solution"}

    def fmt(t: int) -> str:
        return f"{t // 60:02d}:{t % 60:02d}"

    for v, d in enumerate(day_specs):
        idx = routing.Start(v)
        day_plan = {"date": d.date.isoformat(), "stops": [], "meals": 0}

        depot_node = nodes[0]

        while not routing.IsEnd(idx):
            ni = manager.IndexToNode(idx)
            n = nodes[ni]
            tmin = solution.Min(time_dim.CumulVar(idx))

            if n.role != "depot":
                day_plan["stops"].append(
                    {
                        "poi_id": n.poi_id,
                        "name": n.name,
                        "role": n.role,
                        "themes": n.themes if n.role == "attraction" else [],
                        "arrival": fmt(tmin),
                        "start_service": fmt(tmin),
                        "depart": fmt(tmin + n.service),
                        "latitude": n.lat,
                        "longitude": n.lon,
                    }
                )
                if n.role == "meal":
                    day_plan["meals"] += 1

            idx = solution.Value(routing.NextVar(idx))

        end_idx = routing.End(v)
        end_time = solution.Min(time_dim.CumulVar(end_idx))
        day_plan["stops"].append(
            {
                "poi_id": depot_node.poi_id,
                "name": depot_node.name,
                "role": depot_node.role,
                "themes": [],
                "arrival": fmt(end_time),
                "start_service": fmt(end_time),
                "depart": fmt(end_time),
                "latitude": depot_node.lat,
                "longitude": depot_node.lon,
            }
        )

        result["days"].append(day_plan)

    return result


# Main Entry Point


def run_cvrptw(
    maut_output: dict,
    hotel: Dict[str, float],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 15,
) -> dict:
    """
    Run CVRPTW on MAUT output using OR-Tools.

    Uses the same DaySpec/Node/travel model as ACS-CVRPTW, with:
    - OSRM-based travel times.
    - Per-day time windows derived from openHours and pacing.
    - Meal windows restricted to meal-time bands (hard).
    - Soft penalties approximating "no consecutive meals" and "no theme repetition".
    """
    try:
        selected_themes = maut_output.get("meta", {}).get("selected_themes", []) or []
        day_specs, nodes, travel = build_problem(
            maut_output,
            hotel,
            pacing=pacing,
            selected_themes=selected_themes,
            mandatory=mandatory,
        )

        if not day_specs:
            return {
                "days": [],
                "note": (
                    "No day_specs generated. "
                    f"num_days in meta: {maut_output.get('meta', {}).get('num_days')}, "
                    f"dates: {maut_output.get('meta', {}).get('dates')}"
                ),
            }

        if len(nodes) <= 1:
            pr = maut_output.get("meta", {}).get("pois_by_role", {})
            return {
                "days": [],
                "note": (
                    "Only depot node. "
                    f"pois_by_role keys: {list(pr.keys())}, "
                    f"meal count: {len(pr.get('meal', []))}, "
                    f"attraction count: {len(pr.get('attraction', []))}"
                ),
            }

        meal_nodes = sum(1 for n in nodes if n.role == "meal")
        meals_required = (
            min(2, meal_nodes // len(day_specs))
            if meal_nodes > 0 and len(day_specs) > 0
            else 0
        )

        return solve_cvrptw(
            day_specs,
            nodes,
            travel,
            meals_required=meals_required,
            time_limit_sec=time_limit_sec,
        )
    except Exception as e:
        return {"days": [], "note": f"Exception in run_cvrptw: {str(e)}"}
