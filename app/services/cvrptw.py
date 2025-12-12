from __future__ import annotations

from typing import List, Dict, Optional, Tuple
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.services.osrm import tiered_round
from app.services.vrp_model import DaySpec, Node, vrp_config
from app.services.vrp_utils import build_problem, format_time_minutes
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _get_primary_theme(themes: Optional[List[str]]) -> Optional[str]:
    """Return the first theme from a list, if available."""
    return themes[0] if themes else None


def _is_food_like(node: Node) -> bool:
    """Check if a node is meal or has food_culinary theme (for food streak)."""
    if node.role == "meal":
        return True
    if node.themes and "food_culinary" in node.themes:
        return True
    return False


def _get_meal_window_penalty(arrival_min: int, cfg=vrp_config) -> int:
    """
    Calculate penalty for meal scheduled outside preferred windows.
    Windows: breakfast (7-10am), lunch (12-2pm), dinner (6-9pm).
    Returns penalty in cost units.
    """
    SOFT_TOL = 30  # Minutes before penalty applies

    deltas = []
    for w_start, w_end in cfg.meal_windows:
        if w_start <= arrival_min <= w_end:
            deltas.append(0)
        elif arrival_min < w_start:
            deltas.append(w_start - arrival_min)
        else:
            deltas.append(arrival_min - w_end)

    best_delta = min(deltas) if deltas else cfg.meal_hard_tol_min + 1

    # If too far from any meal window, apply large penalty
    if best_delta > cfg.meal_hard_tol_min:
        return cfg.penalty_meal_to_meal * 2  # Very high penalty

    # Soft penalty for being outside tolerance but within hard limit
    if best_delta > SOFT_TOL:
        return int(30.0 * float(best_delta - SOFT_TOL))

    return 0


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
        base_travel_cost = tiered_round(travel[i][j] / 60.0) + nodes[i].service
        penalty = 0

        # Penalize consecutive meals
        if nodes[i].role == "meal" and nodes[j].role == "meal":
            penalty += vrp_config.penalty_meal_to_meal

        # Penalize consecutive food-like POIs (meals + food_culinary attractions)
        if _is_food_like(nodes[i]) and _is_food_like(nodes[j]):
            penalty += vrp_config.penalty_same_theme  # Use same penalty as theme

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

    # Theme dimension to enforce max attractions with same theme per day
    # Note: We only add dimensions for themes with many POIs to avoid solver overload
    unique_themes = list(
        set(
            _get_primary_theme(n.themes)
            for n in nodes
            if n.role == "attraction" and _get_primary_theme(n.themes)
        )
    )

    # Count POIs per theme
    theme_counts = {}
    for n in nodes:
        if n.role == "attraction":
            t = _get_primary_theme(n.themes)
            if t:
                theme_counts[t] = theme_counts.get(t, 0) + 1

    def make_theme_cb(target_theme: str):
        """Create a theme callback with captured theme value."""

        def theme_cb(from_index, to_index):
            j = manager.IndexToNode(to_index)
            node_j = nodes[j]
            if (
                node_j.role == "attraction"
                and _get_primary_theme(node_j.themes) == target_theme
            ):
                return 1
            return 0

        return theme_cb

    # Only add theme dimension for themes that have more than max_per_day POIs
    # This avoids solver overload while still enforcing limits where needed
    for theme in unique_themes:
        if theme_counts.get(theme, 0) > vrp_config.max_theme_per_day:
            theme_transit_idx = routing.RegisterTransitCallback(make_theme_cb(theme))
            routing.AddDimension(
                theme_transit_idx,
                0,  # No slack
                vrp_config.max_theme_per_day,  # Max per day (shared config)
                True,  # Start cumul to zero
                f"theme_{theme}",
            )

    # Note: Food streak (consecutive food items) is handled via soft penalty in transit_cb
    # OR-Tools dimensions track cumulative values, not consecutive sequences

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
            logger.info("CVRPTW: Fallback solve with relaxed meal constraints used.")
        return result
    except Exception as e:
        return {"days": [], "note": f"Exception in run_cvrptw: {str(e)}"}
