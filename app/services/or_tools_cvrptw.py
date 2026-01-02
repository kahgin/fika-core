from __future__ import annotations

from typing import List, Dict, Optional
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.services.vrp_model import DaySpec, Node, vrp_config
from app.services.vrp_utils import build_problem, format_time_minutes
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _get_primary_theme(themes: Optional[List[str]]) -> Optional[str]:
    """Return the first theme from a list, if available."""
    return themes[0] if themes else None


def _get_base_id(poi_id: str) -> str:
    """Strip internal suffixes to get base POI ID.

    Handles:
    - Regular POIs: 'uuid_day0' -> 'uuid'
    - Hotel events: 'uuid_checkin_day0' -> 'uuid'
    - Hotel events: 'uuid_checkout_day0' -> 'uuid'
    - Hotel events: 'uuid_stay_day0' -> 'uuid'
    """
    base = poi_id.rsplit("_day", 1)[0]
    for suffix in ("_checkin", "_checkout", "_stay"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def solve_cvrptw(
    day_specs: List[DaySpec],
    nodes: List[Node],
    travel: List[List[int]],
    meals_required: int = 2,
    time_limit_sec: int = 15,
    slack_wait_min: int = 120,
    user_themes: Optional[List[str]] = None,
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
    - Handles all-day POIs by excluding other POIs from that day.
    """
    if not day_specs:
        return {"days": [], "note": "No days specified"}
    if len(nodes) <= 1:
        return {"days": [], "note": "No POIs available"}

    # Identify days with all-day mandatory POIs
    all_day_days: Dict[int, int] = {}  # day_index -> all_day_node_idx
    for ni, n in enumerate(nodes):
        if n.is_all_day and n.is_mandatory:
            for day_idx in n.windows_by_day.keys():
                all_day_days[day_idx] = ni

    N = len(nodes)
    V = len(day_specs)

    manager = pywrapcp.RoutingIndexManager(N, V, 0)
    routing = pywrapcp.RoutingModel(manager)

    routing_idx_to_node = [manager.IndexToNode(i) for i in range(manager.GetNumberOfIndices())]

    def time_cb(from_index, to_index):
        i = routing_idx_to_node[from_index]
        j = routing_idx_to_node[to_index]
        return int(travel[i][j] + nodes[i].service)

    # Cost callback (minutes): time + soft penalties.
    user_theme_set = set(user_themes or [])

    def cost_cb(from_index, to_index):
        i = routing_idx_to_node[from_index]
        j = routing_idx_to_node[to_index]
        base_cost = int(travel[i][j] + nodes[i].service)
        penalty = 0

        if nodes[i].role == "meal" and nodes[j].role == "meal":
            penalty += vrp_config.penalty_meal_to_meal * 2

        # If the user explicitly picked a single theme, don't penalize repeating it.
        theme_i = _get_primary_theme(nodes[i].themes)
        theme_j = _get_primary_theme(nodes[j].themes)
        if theme_i and theme_j and theme_i == theme_j:
            if not (len(user_theme_set) == 1 and theme_i in user_theme_set):
                penalty += vrp_config.penalty_same_theme

        return base_cost + penalty

    time_idx = routing.RegisterTransitCallback(time_cb)
    cost_idx = routing.RegisterTransitCallback(cost_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_idx)

    # Time dimension
    routing.AddDimension(
        time_idx,
        slack_wait_min,  # waiting/slack
        max(d.end_min for d in day_specs),
        False,
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    for v, d in enumerate(day_specs):
        time_dim.CumulVar(routing.Start(v)).SetRange(d.start_min, d.end_min)
        time_dim.CumulVar(routing.End(v)).SetRange(d.start_min, d.end_min)

    # Node time windows and vehicle assignment
    for ni, n in enumerate(nodes):
        if n.role == "depot":
            continue

        available_days = list(n.windows_by_day.keys())

        if all_day_days:
            filtered_days = []
            for day_v in available_days:
                if day_v in all_day_days:
                    # Only allow accommodation or the all-day POI itself
                    if n.role == "accommodation" or ni == all_day_days[day_v]:
                        filtered_days.append(day_v)
                else:
                    filtered_days.append(day_v)
            available_days = filtered_days

        if not available_days:
            continue

        if len(available_days) == 1:
            day_v = available_days[0]
            routing.SetAllowedVehiclesForIndex([day_v], manager.NodeToIndex(ni))
            a_min, b_max = n.windows_by_day[day_v][0]
            time_dim.CumulVar(manager.NodeToIndex(ni)).SetRange(a_min, b_max)
        else:
            routing.SetAllowedVehiclesForIndex(available_days, manager.NodeToIndex(ni))
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

    for _, idxs in by_poi.items():
        is_mand = any(nodes[i].is_mandatory for i in idxs)
        penalty = vrp_config.mandatory_miss_penalty if is_mand else vrp_config.drop_poi_penalty
        routing.AddDisjunction([manager.NodeToIndex(i) for i in idxs], penalty, 1)

    def meal_cb(from_index, to_index):
        j = routing_idx_to_node[to_index]
        return 1 if nodes[j].role == "meal" else 0

    meal_idx = routing.RegisterTransitCallback(meal_cb)
    routing.AddDimension(
        meal_idx,
        0,
        3,
        True,
        "Meals",
    )
    meal_dim = routing.GetDimensionOrDie("Meals")

    if meals_required > 0:
        for v in range(V):
            available_meals_today = sum(1 for n in nodes if n.role == "meal" and v in n.windows_by_day)
            req = min(meals_required, available_meals_today)
            if req > 0:
                meal_dim.SetCumulVarSoftLowerBound(routing.End(v), req, vrp_config.meal_shortfall_penalty)

    # Search parameters
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
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

        while not routing.IsEnd(idx):
            ni = manager.IndexToNode(idx)
            if ni != 0:  # Skip depot node (index 0)
                n = nodes[ni]
                tvar = time_dim.CumulVar(idx)
                arrival_time = solution.Min(tvar)
                service_start = solution.Min(tvar)
                depart_time = service_start + n.service
                clean_poi_id = _get_base_id(n.poi_id)

                stop_dict = {
                    "poi_id": clean_poi_id,
                    "name": n.name,
                    "role": n.role,
                    "themes": n.themes or [],
                    "arrival": format_time_minutes(arrival_time),
                    "depart": format_time_minutes(depart_time),
                    "latitude": n.lat,
                    "longitude": n.lon,
                }
                if n.images:
                    stop_dict["images"] = n.images
                if n.hotel_event_type:
                    stop_dict["hotel_event_type"] = n.hotel_event_type
                day_plan["stops"].append(stop_dict)

                if n.role == "meal":
                    day_plan["meals"] += 1

            prev_idx = idx
            idx = solution.Value(routing.NextVar(idx))
            total_distance += routing.GetArcCostForVehicle(prev_idx, idx, v)

        result["days"].append(day_plan)

    result["meta"] = {"total_distance": round(total_distance / 60, 2)}
    return result


def run_cvrptw(
    maut_output: dict,
    hotel: Dict[str, float],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    time_limit_sec: int = 15,
    is_first_city: bool = True,
    is_last_city: bool = True,
    prev_city_hotel: Optional[Dict[str, float]] = None,
) -> dict:
    """
    Run CVRPTW on MAUT output using OR-Tools.

    Uses the shared VRP model and configuration, enforcing constraints
    like meal times and theme repetition through the OR-Tools routing model.
    """
    try:
        day_specs, nodes, travel = build_problem(
            maut_output,
            hotel,
            pacing=pacing,
            mandatory=mandatory,
            is_first_city=is_first_city,
            is_last_city=is_last_city,
            prev_city_hotel=prev_city_hotel,
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
        # Prioritizes scheduling 3 meals per day when enough meal nodes are available
        meals_per_day_available = meal_nodes // len(day_specs) if len(day_specs) > 0 else 0
        meals_required = (
            min(3, max(2, meals_per_day_available))
            if meal_nodes >= len(day_specs) * 2
            else min(2, meals_per_day_available)
        )

        selected_themes = maut_output.get("meta", {}).get("selected_themes") or []
        user_themes = [str(t) for t in selected_themes if t] if isinstance(selected_themes, list) else None

        result = solve_cvrptw(
            day_specs,
            nodes,
            travel,
            meals_required=meals_required,
            time_limit_sec=time_limit_sec,
            slack_wait_min=120,
            user_themes=user_themes,
        )
        return result
    except Exception as e:
        return {"days": [], "note": f"Exception in run_cvrptw: {str(e)}"}
