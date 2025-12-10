from __future__ import annotations

import datetime as dt
from typing import List, Dict, Tuple, Optional

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.services.osrm import osrm_client
from app.services.vrp_model import DaySpec, Node, vrp_config
from app.services.vrp_utils import (
    extract_windows_for_date,
    restrict_meal_windows,
    format_time_minutes,
)


def _get_primary_theme(themes: Optional[List[str]]) -> Optional[str]:
    """Return the first theme from a list, if available."""
    return themes[0] if themes else None


def _is_meal_in_preferred_window(start_min: int) -> bool:
    """Check if a meal's start time falls within the preferred lunch or dinner windows."""
    return any(
        w_start <= start_min <= w_end for w_start, w_end in vrp_config.meal_windows[1:]
    )


def _create_day_specs(
    maut_output: dict, hotel: Dict[str, float], pacing: str
) -> List[DaySpec]:
    """Create a list of DaySpec objects based on the trip duration and pacing."""
    meta = maut_output.get("meta", {})
    dates = meta.get("dates", {})
    num_days = meta.get("num_days", 1)

    start_date = dt.date.today()
    if dates.get("type") == "specific":
        start_raw = dates.get("start_date")
        if start_raw:
            try:
                start_date = dt.date.fromisoformat(str(start_raw).split("T")[0])
            except (ValueError, TypeError):
                pass

    day_specs = []
    start_min = vrp_config.pace_day_start_min.get(pacing, 9 * 60)
    budget_min = vrp_config.pace_day_budget_min.get(pacing, 11 * 60)
    end_min = start_min + budget_min

    for k in range(num_days):
        day_specs.append(
            DaySpec(
                day_index=k,
                date=start_date + dt.timedelta(days=k),
                start_min=start_min,
                end_min=end_min,
                depot_id=str(hotel["id"]),
            )
        )
    return day_specs


def _create_nodes(
    maut_output: dict,
    day_specs: List[DaySpec],
    hotel: Dict[str, float],
    pacing: str,
    selected_themes: Optional[List[str]] = None,
    mandatory: Optional[Dict[str, Dict]] = None,
) -> List[Node]:
    """Create a list of all nodes (depot and POIs) for the VRP."""
    nodes: List[Node] = []
    idx = 0

    # Depot node (index 0)
    nodes.append(
        Node(
            idx=idx,
            poi_id=str(hotel["id"]),
            name=str(hotel["name"]),
            role="depot",
            lat=float(hotel["lat"]),
            lon=float(hotel["lon"]),
            service=0,
            themes=None,
            windows_by_day={d.day_index: [(d.start_min, d.end_min)] for d in day_specs},
        )
    )
    idx += 1

    # POI nodes
    places = maut_output.get("places", [])
    for poi in places:
        roles = poi.get("poi_roles", [])
        role = "attraction"  # Default role
        if "meal" in roles:
            role = "meal"
        elif "accommodation" in roles:
            # Skip accommodations as they are handled as depots
            continue

        # Filter attractions by selected themes if provided
        if role == "attraction" and selected_themes:
            poi_themes = poi.get("themes") or []
            if not any(t in poi_themes for t in selected_themes):
                continue

        # Each POI can be visited on any day, so create a version for each day
        for day_idx in range(len(day_specs)):
            poi_copy = poi.copy()
            poi_copy["id"] = f"{poi['id']}_day{day_idx}"
            poi_copy["_day_specific"] = day_idx
            new_node = _create_poi_node(
                poi=poi_copy,
                role=role,
                idx=idx,
                day_specs=day_specs,
                pacing=pacing,
                mandatory=mandatory,
            )
            if new_node:
                nodes.append(new_node)
                idx += 1

    return nodes


def _create_poi_node(
    poi: Dict,
    role: str,
    idx: int,
    day_specs: List[DaySpec],
    pacing: str,
    mandatory: Optional[Dict[str, Dict]],
) -> Optional[Node]:
    """Create a single Node object for a POI, handling its schedule and constraints.

    Handles mandatory POI time_type modes:
    - 'specific': Use provided start_time/end_time window
    - 'all_day': Block entire day (day start to end)
    - 'any_time': Use role-based default windows (fallback)
    """
    coords = poi.get("coordinates")
    if not coords or coords.get("lat") is None or coords.get("lng") is None:
        return None

    service_times = vrp_config.service_time_min.get(role, {})
    service = service_times.get(pacing, 90)

    wbd: Dict[int, List[Tuple[int, int]]] = {}
    # Internal data uses snake_case
    open_hours = poi.get("open_hours")
    day_specific = poi.get("_day_specific")
    role_default = vrp_config.default_role_windows.get(role, (9 * 60, 21 * 60))

    # Check if this POI is mandatory and get its constraints
    base_id = str(poi["id"]).rsplit("_day", 1)[0]
    is_mand = False
    md_spec: Dict = {}

    if mandatory and base_id in mandatory:
        is_mand = True
        md_spec = mandatory[base_id] or {}

    # Get mandatory constraints
    day_constraint = md_spec.get("day")
    time_type = md_spec.get("time_type", "any_time")
    is_all_day = md_spec.get("all_day", False) or time_type == "all_day"
    window_constraint = md_spec.get("window")

    # If mandatory with day constraint, only create node for that specific day
    if is_mand and day_constraint is not None:
        target_day = int(day_constraint) - 1  # API uses 1-based indexing
        if day_specific != target_day:
            return None  # This node copy is for the wrong day

    if day_specific is not None:
        d = day_specs[day_specific]

        if is_mand and is_all_day:
            # All-day: block entire day window, use full day budget
            wbd[day_specific] = [(d.start_min, d.end_min)]
            # Set service time to fill the day (minus buffer for travel)
            service = max(service, d.end_min - d.start_min - 60)
        elif is_mand and window_constraint:
            # Specific time window from user
            try:
                start_parts = window_constraint[0].split(":")
                end_parts = window_constraint[1].split(":")
                start = (
                    int(start_parts[0]) * 60 + int(start_parts[1])
                    if len(start_parts) > 1
                    else int(start_parts[0]) * 60
                )
                end = (
                    int(end_parts[0]) * 60 + int(end_parts[1])
                    if len(end_parts) > 1
                    else int(end_parts[0]) * 60
                )
                wbd[day_specific] = [(start, end)]
                # Adjust service time to fit within window
                service = min(service, end - start)
            except (ValueError, IndexError):
                # Invalid format, fall back to role defaults
                day_default = (
                    max(d.start_min, role_default[0]),
                    min(d.end_min, role_default[1]),
                )
                windows = extract_windows_for_date(open_hours, d.date, day_default)
                if role == "meal":
                    windows = restrict_meal_windows(windows)
                if windows:
                    wbd[day_specific] = windows
        else:
            # any_time or no constraint: use role-based defaults
            day_default = (
                max(d.start_min, role_default[0]),
                min(d.end_min, role_default[1]),
            )
            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if role == "meal":
                windows = restrict_meal_windows(windows)
            if windows:
                wbd[day_specific] = windows

        if not wbd:
            return None  # Not visitable on its specific day
    else:
        # This branch is less likely if _create_nodes creates day-specific copies
        for d in day_specs:
            day_default = (
                max(d.start_min, role_default[0]),
                min(d.end_min, role_default[1]),
            )
            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if role == "meal":
                windows = restrict_meal_windows(windows)
            if windows:
                wbd[d.day_index] = windows
        if not wbd:
            return None  # Not visitable on any day

    return Node(
        idx=idx,
        poi_id=str(poi["id"]),
        name=str(poi.get("name")),
        role=role,
        themes=poi.get("themes", []),
        lat=float(coords["lat"]),
        lon=float(coords["lng"]),
        service=service,
        windows_by_day=wbd,
        is_mandatory=is_mand,
    )


# Build Problem from MAUT Output


def build_problem(
    maut_output: dict,
    hotel: Dict[str, float],
    pacing: str = "balanced",
    selected_themes: Optional[List[str]] = None,
    mandatory: Optional[Dict[str, Dict]] = None,
) -> Tuple[List[DaySpec], List[Node], List[List[int]]]:
    """
    Convert MAUT output to the VRP problem format (DaySpecs, Nodes, Travel Matrix).
    This function is now a simplified entry point that delegates to helpers.
    """
    day_specs = _create_day_specs(maut_output, hotel, pacing)
    nodes = _create_nodes(
        maut_output, day_specs, hotel, pacing, selected_themes, mandatory
    )

    # Create the travel matrix using OSRM
    coords = [(n.lat, n.lon) for n in nodes]
    travel_matrix = osrm_client.matrix_minutes(coords)

    return day_specs, nodes, travel_matrix


# OR-Tools Solver


def solve_cvrptw(
    day_specs: List[DaySpec],
    nodes: List[Node],
    travel: List[List[int]],
    meals_required: int = 2,
    time_limit_sec: int = 15,
    slack_wait_min: int = 120,
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

    # Transit callback with penalties
    def transit_cb(from_index, to_index):
        i, j = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        base_travel_cost = travel[i][j] + nodes[i].service
        penalty = 0

        # Penalize consecutive meals
        if nodes[i].role == "meal" and nodes[j].role == "meal":
            penalty += vrp_config.penalty_meal_to_meal

        # Penalize consecutive POIs with the same primary theme
        theme_i = _get_primary_theme(nodes[i].themes)
        theme_j = _get_primary_theme(nodes[j].themes)
        if theme_i and theme_j and theme_i == theme_j:
            penalty += vrp_config.penalty_same_theme

        return base_travel_cost + penalty

    t_idx = routing.RegisterTransitCallback(transit_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(t_idx)

    # Time dimension
    routing.AddDimension(
        t_idx,
        slack_wait_min,  # waiting/slack
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
        is_mand = any(nodes[i].is_mandatory for i in idxs)
        penalty = (
            vrp_config.mandatory_miss_penalty
            if is_mand
            else vrp_config.drop_poi_penalty
        )
        routing.AddDisjunction([manager.NodeToIndex(i) for i in idxs], penalty, 1)

    # Meals dimension (min/max meals per day, cap 3)
    def meal_cb(from_index, to_index):
        j = manager.IndexToNode(to_index)
        return 1 if nodes[j].role == "meal" else 0

    meal_idx = routing.RegisterTransitCallback(meal_cb)
    routing.AddDimension(
        meal_idx,
        0,
        3,
        True,
        "Meals",  # Max 3 meals per day
    )
    meal_dim = routing.GetDimensionOrDie("Meals")

    # Theme dimension to enforce max 2 attractions with same theme per day
    unique_themes = list(
        set(
            _get_primary_theme(n.themes)
            for n in nodes
            if n.role == "attraction" and _get_primary_theme(n.themes)
        )
    )

    for theme in unique_themes:

        def theme_cb(from_index, to_index):
            j = manager.IndexToNode(to_index)
            node_j = nodes[j]
            if (
                node_j.role == "attraction"
                and _get_primary_theme(node_j.themes) == theme
            ):
                return 1
            return 0

        theme_transit_idx = routing.RegisterTransitCallback(theme_cb)
        routing.AddDimension(
            theme_transit_idx,
            0,  # No slack
            vrp_config.acs_max_theme_per_day,  # Max 2 per day
            True,  # Start cumul to zero
            f"theme_{theme}",
        )

    # Set meal requirements per day
    if meals_required > 0:
        for v in range(V):
            # Count available meal nodes for the specific day
            available_meals_today = sum(
                1 for n in nodes if n.role == "meal" and v in n.windows_by_day
            )
            req = min(meals_required, available_meals_today)
            if req > 0:
                meal_dim.CumulVar(routing.End(v)).SetRange(req, 3)

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

    total_distance = 0
    for v, d in enumerate(day_specs):
        idx = routing.Start(v)
        day_plan = {"date": d.date.isoformat(), "stops": [], "meals": 0}
        depot_node = nodes[0]

        # Add depot start
        day_plan["stops"].append(
            {
                "poi_id": depot_node.poi_id,
                "name": depot_node.name,
                "role": depot_node.role,
                "arrival": format_time_minutes(d.start_min),
                "start_service": format_time_minutes(d.start_min),
                "depart": format_time_minutes(d.start_min),
            }
        )

        while not routing.IsEnd(idx):
            ni = manager.IndexToNode(idx)
            if ni != 0:  # Skip the depot start, which is handled already
                n = nodes[ni]
                tvar = time_dim.CumulVar(idx)
                arrival_time = solution.Min(tvar)
                service_start = solution.Min(tvar)
                depart_time = service_start + n.service

                day_plan["stops"].append(
                    {
                        "poi_id": n.poi_id,
                        "name": n.name,
                        "role": n.role,
                        "themes": n.themes or [],
                        "arrival": format_time_minutes(arrival_time),
                        "start_service": format_time_minutes(service_start),
                        "depart": format_time_minutes(depart_time),
                    }
                )
                if n.role == "meal":
                    day_plan["meals"] += 1

            prev_idx = idx
            idx = solution.Value(routing.NextVar(idx))
            total_distance += routing.GetArcCostForVehicle(prev_idx, idx, v)

        # Add depot end
        end_time = solution.Min(time_dim.CumulVar(routing.End(v)))
        day_plan["stops"].append(
            {
                "poi_id": depot_node.poi_id,
                "name": depot_node.name,
                "role": depot_node.role,
                "arrival": format_time_minutes(end_time),
                "start_service": format_time_minutes(end_time),
                "depart": format_time_minutes(end_time),
            }
        )

        result["days"].append(day_plan)

    result["meta"] = {"total_distance": round(total_distance / 60, 2)}
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

    This version uses the shared VRP model and configuration, enforcing constraints
    like meal times and theme repetition through the OR-Tools routing model.
    """
    try:
        day_specs, nodes, travel = build_problem(
            maut_output, hotel, pacing=pacing, mandatory=mandatory
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
            return {
                "days": [],
                "note": "Only depot node available.",
            }

        meal_nodes = sum(1 for n in nodes if n.role == "meal")
        meals_required = (
            min(3, meal_nodes // len(day_specs))
            if meal_nodes > 0 and len(day_specs) > 0
            else 0
        )

        result = solve_cvrptw(
            day_specs,
            nodes,
            travel,
            meals_required=meals_required,
            time_limit_sec=time_limit_sec,
            slack_wait_min=120,
        )
        # Fallback: relax constraints if infeasible (no days)
        if not result.get("days"):
            result = solve_cvrptw(
                day_specs,
                nodes,
                travel,
                meals_required=0,
                time_limit_sec=max(10, time_limit_sec),
                slack_wait_min=300,
            )
        return result
    except Exception as e:
        return {"days": [], "note": f"Exception in run_cvrptw: {str(e)}"}
