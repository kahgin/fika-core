from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from math import radians, sin, cos, sqrt, atan2
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# Configuration

PACE_DAY_BUDGET_MIN = {
    "relaxed": 9 * 60,  # 09:00-18:00
    "balanced": 11 * 60,  # 09:00-20:00
    "packed": 13 * 60,  # 09:00-22:00
}

SERVICE_TIME = {
    "attraction": {"relaxed": 120, "balanced": 90, "packed": 60},
    "meal": {"relaxed": 75, "balanced": 60, "packed": 45},
    "accommodation": {"relaxed": 0, "balanced": 0, "packed": 0},
}

LUNCH_WIN = (12 * 60, 14 * 60)
DINNER_WIN = (18 * 60, 21 * 60)

PENALTY_MEAL_TO_MEAL = 40
PENALTY_SAME_THEME = 15
DROP_PENALTY_BASE = 2000


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
    theme: Optional[str]
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
            b = 24 * 60
        return (a, b)
    except:
        return None


def weekday_name(d: dt.date) -> str:
    """Return weekday name like 'Monday'."""
    return d.strftime("%A")


def minutes(hhmm: str) -> int:
    """Convert 'HH:MM' to minutes from midnight."""
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def haversine_minutes(
    a: Tuple[float, float], b: Tuple[float, float], speed_kmh=30
) -> int:
    """Calculate travel time in minutes between two coordinates."""
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    u = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    km = R * 2 * atan2(sqrt(u), sqrt(1 - u))
    return max(0, int(round((km / speed_kmh) * 60)))


def pick_theme(categories: List[str], selected_themes: List[str]) -> Optional[str]:
    """Pick first matching theme from POI categories."""
    cat_s = " ".join(categories or []).lower()
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
    for lab in raw:
        rng = parse_time_range_label(lab)
        if not rng:
            continue
        a, b = rng
        a1, b1 = max(a, d_start), min(b, d_end)
        if a1 <= b1:
            out.append((a1, b1))

    return out or [default_window]


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

    Args:
        maut_output: Output from run_pipeline() with places, meta, etc.
        hotel: {"id": str, "name": str, "lat": float, "lon": float}
        pacing: "relaxed" | "balanced" | "packed"
        selected_themes: List of theme keys
        mandatory: {poi_id: {"day": int, "window": ["HH:MM", "HH:MM"]}}

    Returns:
        (day_specs, nodes, travel_matrix)
    """
    # Extract dates and num_days
    meta = maut_output.get("meta", {})
    dates = meta.get("dates", {})

    # num_days can be in meta or at root level
    num_days = meta.get("num_days") or maut_output.get("num_days", 3)

    # If not found, calculate from by_role or places count
    if not num_days:
        num_days = 3  # default

    # Parse dates
    if dates.get("type") == "specific" and dates.get("startDate"):
        start = dt.date.fromisoformat(dates["startDate"])
        if dates.get("endDate"):
            end = dt.date.fromisoformat(dates["endDate"])
            num_days = (end - start).days + 1
    else:
        start = dt.date.today()

    # Build day specs
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
        theme=None,
        windows_by_day={d.day_index: [(d.start_min, d.end_min)] for d in day_specs},
    )
    nodes.append(depot)
    idx += 1

    # POI nodes - use structured pois_by_role if available, else fall back to places
    sel_themes = selected_themes or maut_output.get("meta", {}).get(
        "selected_themes", []
    )
    pois_by_role = meta.get("pois_by_role", {})

    # If structured by role, use that; otherwise use flat places list
    if pois_by_role:
        # Process each role separately to ensure proper distribution
        for role in ["meal", "attraction", "accommodation"]:
            role_pois = pois_by_role.get(role, [])

            # Skip accommodation - hotel depot serves as accommodation
            if role == "accommodation":
                continue

            for poi in role_pois:
                # Create separate node for each day the POI is available
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
        # Fallback to flat places list
        places = maut_output.get("places", [])
        for poi in places:
            # Determine role
            roles = poi.get("poi_roles", [])
            if "meal" in roles:
                role = "meal"
            elif "accommodation" in roles:
                role = "accommodation"
            else:
                role = "attraction"

            # Create separate node for each day
            for day_idx in range(num_days):
                poi_copy = poi.copy()
                poi_copy["id"] = f"{poi['id']}_day{day_idx}"
                poi_copy["_day_specific"] = day_idx
                _add_poi_node(
                    poi_copy, role, nodes, idx, day_specs, pacing, sel_themes, mandatory
                )
                idx += 1

    # Build travel matrix
    coords = [(n.lat, n.lon) for n in nodes]
    N = len(nodes)
    travel = [[0] * N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j:
                travel[i][j] = haversine_minutes(coords[i], coords[j], speed_kmh=25)

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
    theme = pick_theme(poi.get("categories", []), sel_themes)

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

    # Build per-day windows
    wbd: Dict[int, List[Tuple[int, int]]] = {}

    # Check if this is a day-specific POI (like hotel accommodation)
    day_specific = poi.get("_day_specific")
    if day_specific is not None:
        # Only available on specific day
        d = day_specs[day_specific]

        # Check if POI is open on this specific day
        open_hours = poi.get("openHours") or poi.get("hours")
        if open_hours:
            weekday = d.date.strftime("%A")
            day_hours = open_hours.get(weekday, [])
            # Skip if closed on this day
            if not day_hours or "closed" in str(day_hours).lower():
                return

        wbd = {day_specific: [(d.start_min, d.end_min)]}
    else:
        # Available on all days - check each day
        for d in day_specs:
            day_default = (d.start_min, d.end_min)
            open_hours = poi.get("openHours") or poi.get("hours")

            # Check if POI is open on this day
            if open_hours:
                weekday = d.date.strftime("%A")
                day_hours = open_hours.get(weekday, [])
                # Skip this day if closed
                if not day_hours or "closed" in str(day_hours).lower():
                    continue

            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if windows:
                wbd[d.day_index] = windows

        # If POI is closed on all days, don't add it
        if not wbd:
            return

    # Check if mandatory
    is_mand = bool(mandatory and poi["id"] in mandatory)
    if is_mand:
        md_spec = mandatory[poi["id"]]
        dk = int(md_spec["day"]) - 1
        a = minutes(md_spec["window"][0])
        b = minutes(md_spec["window"][1])
        wbd = {dk: [(a, b)]}

    nodes.append(
        Node(
            idx=idx,
            poi_id=poi["id"],
            name=poi["name"],
            role=role,
            lat=float(lat),
            lon=float(lon),
            service=service,
            theme=theme,
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

    Args:
        day_specs: List of day specifications
        nodes: List of nodes (depot + POIs)
        travel: Travel time matrix
        meals_required: Minimum meals per day
        time_limit_sec: Solver time limit

    Returns:
        {"days": [{"date": str, "stops": [...], "meals": int}]}
    """
    N = len(nodes)
    V = len(day_specs)

    manager = pywrapcp.RoutingIndexManager(N, V, 0)
    routing = pywrapcp.RoutingModel(manager)

    # Transit callback
    def transit_cb(from_index, to_index):
        i, j = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        base = travel[i][j] + nodes[i].service
        bonus = 0
        if nodes[i].role == "meal" and nodes[j].role == "meal":
            bonus += PENALTY_MEAL_TO_MEAL
        if nodes[i].theme and nodes[j].theme and nodes[i].theme == nodes[j].theme:
            bonus += PENALTY_SAME_THEME
        return base + bonus

    t_idx = routing.RegisterTransitCallback(transit_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(t_idx)

    # Time dimension
    routing.AddDimension(t_idx, 120, max(d.end_min for d in day_specs), False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    # Depot time windows
    for v, d in enumerate(day_specs):
        time_dim.CumulVar(routing.Start(v)).SetRange(d.start_min, d.end_min)
        time_dim.CumulVar(routing.End(v)).SetRange(d.start_min, d.end_min)

    # Node time windows and vehicle assignment
    for ni, n in enumerate(nodes):
        if n.role == "depot":
            continue

        # If POI is available on multiple days, allow any vehicle
        # If POI is day-specific (like hotel accommodation), restrict to that day
        available_days = list(n.windows_by_day.keys())

        if len(available_days) == 1:
            # Day-specific POI - restrict to that vehicle/day
            day_v = available_days[0]
            routing.SetAllowedVehiclesForIndex([day_v], manager.NodeToIndex(ni))
            a_min, b_max = n.windows_by_day[day_v][0]
            time_dim.CumulVar(manager.NodeToIndex(ni)).SetRange(a_min, b_max)
        else:
            # Multi-day POI - can be visited by any vehicle, set time windows per vehicle
            for day_v in available_days:
                if n.windows_by_day[day_v]:
                    a_min, b_max = n.windows_by_day[day_v][0]
                    # Set time window for this vehicle
                    time_dim.CumulVar(manager.NodeToIndex(ni)).SetRange(a_min, b_max)

    # Disjunctions (visit at most once) - group by base POI ID without _dayX suffix
    by_poi: Dict[str, List[int]] = {}
    for i, n in enumerate(nodes):
        if n.role != "depot":
            # Strip _dayX suffix to group all copies of same POI
            base_id = n.poi_id.rsplit("_day", 1)[0]
            by_poi.setdefault(base_id, []).append(i)

    for poi_id, idxs in by_poi.items():
        any_mand = any(nodes[i].is_mandatory for i in idxs)
        penalty = 10_000_000 if any_mand else DROP_PENALTY_BASE
        routing.AddDisjunction([manager.NodeToIndex(i) for i in idxs], penalty, 1)

    # Meals dimension with requirement - cap at 3 meals per day
    def meal_cb(from_index, to_index):
        j = manager.IndexToNode(to_index)
        return 1 if nodes[j].role == "meal" else 0

    meal_idx = routing.RegisterTransitCallback(meal_cb)
    routing.AddDimension(meal_idx, 0, 3, True, "Meals")  # Max 3 meals per day
    meal_dim = routing.GetDimensionOrDie("Meals")

    # Enforce meal requirements per day (min and max)
    if meals_required > 0:
        for v in range(V):
            available_meals = sum(
                1
                for ni, n in enumerate(nodes)
                if n.role == "meal"
                and (len(n.windows_by_day) > 1 or v in n.windows_by_day)
            )
            req_min = min(meals_required, available_meals)
            req_max = min(3, available_meals)  # Cap at 3 meals
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

    # Build result
    result = {"days": []}
    if not solution:
        return {"days": [], "note": "No feasible solution"}

    def fmt(t):
        return f"{t // 60:02d}:{t % 60:02d}"

    for v, d in enumerate(day_specs):
        idx = routing.Start(v)
        day_plan = {"date": d.date.isoformat(), "stops": [], "meals": 0}

        # Track depot node for hotel return
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
                        "arrival": fmt(tmin),
                        "start_service": fmt(tmin),
                        "depart": fmt(tmin + n.service),
                    }
                )
                if n.role == "meal":
                    day_plan["meals"] += 1
            idx = solution.Value(routing.NextVar(idx))

        # Add hotel return at end of day
        end_idx = routing.End(v)
        end_time = solution.Min(time_dim.CumulVar(end_idx))
        day_plan["stops"].append(
            {
                "poi_id": depot_node.poi_id,
                "name": depot_node.name,
                "role": "hotel",
                "arrival": fmt(end_time),
                "start_service": fmt(end_time),
                "depart": fmt(end_time),
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
    Run CVRPTW on MAUT output.

    Args:
        maut_output: Output from maut.run_pipeline()
        hotel: {"id": str, "name": str, "lat": float, "lon": float}
        pacing: "relaxed" | "balanced" | "packed"
        mandatory: {poi_id: {"day": int, "window": ["HH:MM", "HH:MM"]}}
        time_limit_sec: Solver time limit

    Returns:
        {"days": [{"date": str, "stops": [...], "meals": int}]}
    """
    selected_themes = maut_output.get("meta", {}).get("selected_themes", [])
    day_specs, nodes, travel = build_problem(
        maut_output,
        hotel,
        pacing=pacing,
        selected_themes=selected_themes,
        mandatory=mandatory,
    )
    # Reduce meal requirement to 1 per day to make problem more feasible
    return solve_cvrptw(
        day_specs, nodes, travel, meals_required=1, time_limit_sec=time_limit_sec
    )
