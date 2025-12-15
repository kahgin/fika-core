"""
ACS-based solver for CVRPTW (Capacitated Vehicle Routing Problem with Time Windows).

This module implements an Ant Colony System algorithm optimized for multi-day
itinerary planning with constraints including:
- Time windows for each POI
- Service times at each stop
- Meal scheduling requirements (min 2 per day)
- Theme diversity constraints
- Mandatory POI requirements
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Set, Any

from app.services.osrm import tiered_round
from app.services.vrp_model import VRPConfig
from app.services.vrp_model import DaySpec, Node, vrp_config
from app.services.vrp_utils import format_time_minutes


@dataclass(slots=True)
class DayRoute:
    """Represents the output of a single-day ACS optimization."""

    date: str
    stops: List[Dict]
    meals: int
    total_cost: float
    total_distance: float
    visited_base_ids: Set[str]
    infeasible: bool = False


def _get_base_id(poi_id: str) -> str:
    """Strip the '_dayX' suffix to get the base POI ID."""
    return poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id


def _get_primary_theme(node: Node) -> Optional[str]:
    """
    Return the primary theme for a node, used for theme-repetition rules.
    If a themes list exists, the first theme is considered primary.
    """
    if node.themes:
        return node.themes[0]
    return node.role


ROLE_DEPOT_INTERNAL = "depot"
ROLE_ACCOMMODATION = "accommodation"


def _simulate_day_route(
    day: DaySpec,
    nodes: List[Node],
    travel: List[List[int]],
    order: List[int],
    day_index: int,
    meals_min: int,
    mandatory_for_day: Set[str],
    cfg: VRPConfig,
    user_themes: Optional[List[str]] = None,
) -> Tuple[float, float, List[Dict], Set[str], int]:
    """
    Simulate a single day's route to calculate its feasibility and cost.

    This function enforces all dynamic constraints of the ACS model, including:
    - Time windows and day start/end horizons.
    - Service times at each POI.
    - Meal constraints (max per day, no consecutive meals, preferred times).
    - Theme diversity (max attractions with the same theme).
    - Penalties for missed mandatory POIs and meal shortfalls.

    Returns a tuple containing the route's cost, distance, stops, visited POI IDs,
    and the number of meals included.
    """
    depot_idx = 0
    depot_node = nodes[depot_idx]
    t = day.start_min
    current = depot_idx
    stops: List[Dict] = []
    visited_base_ids: Set[str] = set()
    meals_count = 0
    meals_in_window = 0
    food_streak = 0
    total_travel_min = 0
    last_role: str = ROLE_DEPOT_INTERNAL
    theme_count_per_day: Dict[str, int] = {}
    extra_penalty_total = 0.0

    stops.append(
        {
            "poi_id": depot_node.poi_id,
            "name": depot_node.name,
            "role": ROLE_ACCOMMODATION,
            "themes": [],
            "arrival": format_time_minutes(day.start_min),
            "start_service": format_time_minutes(day.start_min),
            "depart": format_time_minutes(day.start_min),
            "latitude": depot_node.lat,
            "longitude": depot_node.lon,
        }
    )

    SOFT_TOL = 30

    for node_idx in order:
        n = nodes[node_idx]

        if day_index not in n.windows_by_day:
            continue

        primary_theme = _get_primary_theme(n)

        if n.role == "meal" and meals_count >= 3:
            continue

        is_meal = n.role == "meal"

        if is_meal and food_streak >= 2:
            continue

        if n.role == "meal" and last_role == "meal":
            continue

        travel_min = travel[current][node_idx]
        arrival = t + travel_min

        extra_penalty = 0.0
        meal_is_in_window = False
        if n.role == "meal":
            deltas = []
            for start, end in cfg.meal_windows:
                if start <= arrival <= end:
                    deltas.append(0)
                    meal_is_in_window = True
                elif arrival < start:
                    deltas.append(start - arrival)
                else:
                    deltas.append(arrival - end)

            best_delta = min(deltas) if deltas else cfg.meal_hard_tol_min + 1

            if best_delta > cfg.meal_hard_tol_min:
                continue

            if best_delta > SOFT_TOL:
                extra_penalty = 30.0 * float(best_delta - SOFT_TOL)

        chosen_window: Optional[Tuple[int, int]] = None
        start_service: Optional[int] = None
        finish_service: Optional[int] = None

        for w_start, w_end in n.windows_by_day[day_index]:
            if arrival > w_end:
                continue
            start_service = max(arrival, w_start)
            finish_service = start_service + n.service
            if finish_service <= w_end:
                chosen_window = (w_start, w_end)
                break

        if not chosen_window or start_service is None or finish_service is None:
            continue

        back_to_depot = travel[node_idx][depot_idx]
        if finish_service + back_to_depot > day.end_min:
            continue

        t = finish_service
        total_travel_min += travel_min
        current = node_idx
        extra_penalty_total += extra_penalty

        arrival_display = max(arrival, chosen_window[0])

        stops.append(
            {
                "poi_id": n.poi_id,
                "name": n.name,
                "role": n.role,
                "themes": n.themes or [],
                "arrival": format_time_minutes(arrival_display),
                "start_service": format_time_minutes(start_service),
                "depart": format_time_minutes(finish_service),
            }
        )

        if is_meal:
            food_streak += 1
        else:
            food_streak = 0

        visited_base_ids.add(_get_base_id(n.poi_id))
        if n.role == "meal":
            meals_count += 1
            if meal_is_in_window:
                meals_in_window += 1
        last_role = n.role

        if n.role == "attraction" and primary_theme:
            theme_count_per_day[primary_theme] = theme_count_per_day.get(primary_theme, 0) + 1

    back_min = travel[current][depot_idx]
    if t + back_min > day.end_min:
        return float("inf"), 0.0, [], set(), 0

    total_travel_min += back_min
    depot_node = nodes[depot_idx]
    arrival_back = t + back_min
    stops.append(
        {
            "poi_id": depot_node.poi_id,
            "name": depot_node.name,
            "role": ROLE_ACCOMMODATION,
            "themes": [],
            "arrival": format_time_minutes(arrival_back),
            "start_service": format_time_minutes(arrival_back),
            "depart": format_time_minutes(arrival_back),
            "latitude": depot_node.lat,
            "longitude": depot_node.lon,
        }
    )

    cost = float(total_travel_min) + extra_penalty_total

    poi_count = len(visited_base_ids)
    cost -= cfg.poi_visit_bonus * poi_count

    visited_themes = set(theme_count_per_day.keys())

    if user_themes:
        user_theme_set = set(user_themes)
        covered_user_themes = visited_themes & user_theme_set
        cost -= cfg.theme_diversity_bonus * len(covered_user_themes)

        if len(user_themes) > 1 and len(covered_user_themes) > 0:
            counts = [theme_count_per_day.get(t, 0) for t in covered_user_themes]
            if counts:
                max_count = max(counts)
                min_count = min(counts) if min(counts) > 0 else 0
                if max_count > 0 and min_count > 0 and max_count > 3 * min_count:
                    cost += cfg.theme_concentration_penalty
    else:
        cost -= cfg.theme_diversity_bonus * len(visited_themes)

    if meals_min > 0 and meals_count < meals_min:
        meal_shortfall = meals_min - meals_count
        cost += cfg.meal_shortfall_penalty * meal_shortfall

    cost -= cfg.meal_window_bonus * meals_in_window

    if mandatory_for_day:
        missed_mandatory = mandatory_for_day - (visited_base_ids & mandatory_for_day)
        if missed_mandatory:
            cost += cfg.mandatory_miss_penalty * len(missed_mandatory)

    distance_km_equiv = total_travel_min / 60.0

    return cost, distance_km_equiv, stops, visited_base_ids, meals_count


def _two_opt_improve(
    order: List[int],
    nodes: List[Node],
    travel: List[List[int]],
    day: DaySpec,
    day_index: int,
    meals_min: int,
    mandatory_for_day: Set[str],
    cfg: VRPConfig,
    user_themes: Optional[List[str]] = None,
    max_iter: int = 100,
) -> List[int]:
    """Simple 2-opt local search to improve a given route order."""
    best = order[:]
    best_cost, _, _, _, _ = _simulate_day_route(
        day=day,
        nodes=nodes,
        travel=travel,
        order=best,
        day_index=day_index,
        meals_min=meals_min,
        mandatory_for_day=mandatory_for_day,
        cfg=cfg,
        user_themes=user_themes,
    )
    if math.isinf(best_cost):
        return order

    improved = True
    it = 0
    while improved and it < max_iter:
        improved = False
        it += 1
        n = len(best)
        for i in range(n - 1):
            for j in range(i + 1, n):
                new_order = best[:i] + best[i:j][::-1] + best[j:]
                cost, _, _, _, _ = _simulate_day_route(
                    day=day,
                    nodes=nodes,
                    travel=travel,
                    order=new_order,
                    day_index=day_index,
                    meals_min=meals_min,
                    mandatory_for_day=mandatory_for_day,
                    cfg=cfg,
                    user_themes=user_themes,
                )
                if cost < best_cost:
                    best = new_order
                    best_cost = cost
                    improved = True
                    break
            if improved:
                break
    return best


def _acs_optimize_day(
    day: DaySpec,
    nodes: List[Node],
    travel: List[List[int]],
    day_index: int,
    available_node_indices: List[int],
    meals_required: int,
    mandatory_for_day: Set[str],
    cfg: VRPConfig,
    user_themes: Optional[List[str]] = None,
) -> DayRoute:
    """
    Run the Ant Colony System optimization for a single day.
    """
    if not available_node_indices:
        depot = nodes[0]
        return DayRoute(
            date=day.date.isoformat(),
            stops=[
                {
                    "poi_id": depot.poi_id,
                    "name": depot.name,
                    "role": ROLE_ACCOMMODATION,
                    "arrival": format_time_minutes(day.start_min),
                    "start_service": format_time_minutes(day.start_min),
                    "depart": format_time_minutes(day.start_min),
                    "latitude": depot.lat,
                    "longitude": depot.lon,
                }
            ],
            meals=0,
            total_cost=float("inf"),
            total_distance=0.0,
            visited_base_ids=set(),
            infeasible=True,
        )

    subset = available_node_indices
    m = len(subset)

    distances = [[tiered_round(float(travel[subset[i]][subset[j]]) / 60.0) for j in range(m)] for i in range(m)]

    pheromone = [[1.0] * m for _ in range(m)]
    heuristic = [[1.0 / (d + 1e-6) if d > 0 else 0.0 for d in row] for row in distances]

    maut_scores = [nodes[subset[i]].maut_score for i in range(m)]
    max_score = max(maut_scores) if maut_scores else 1.0
    min_score = min(maut_scores) if maut_scores else 0.0
    score_range = max_score - min_score if max_score > min_score else 1.0

    for i in range(m):
        normalized_score = 0.5 + (maut_scores[i] - min_score) / score_range
        for j in range(m):
            heuristic[i][j] *= normalized_score

    best_cost = float("inf")
    best_order: List[int] = []

    meal_local_indices = [i for i in range(m) if nodes[subset[i]].role == "meal"]

    no_improvement_count = 0
    max_no_improvement = 10

    for iteration in range(cfg.acs_n_iterations):
        solutions = []

        for ant_idx in range(cfg.acs_n_ants):
            remaining = list(range(m))

            if meal_local_indices and ant_idx % 3 == 0:
                meal_starts = [i for i in meal_local_indices if i in remaining]
                if meal_starts:
                    current = random.choice(meal_starts)
                else:
                    current = random.choice(remaining)
            else:
                current = random.choice(remaining)

            tour_local = [current]
            remaining.remove(current)

            meals_in_tour = 1 if nodes[subset[current]].role == "meal" else 0

            themes_in_tour: Set[str] = set()
            theme_counts_tour: Dict[str, int] = {}
            start_theme = _get_primary_theme(nodes[subset[current]])
            if start_theme:
                themes_in_tour.add(start_theme)
                theme_counts_tour[start_theme] = 1

            while remaining:
                probs = []
                denom = 0.0
                for j in remaining:
                    tau = pheromone[current][j] ** cfg.acs_alpha
                    eta = heuristic[current][j] ** cfg.acs_beta

                    meal_boost = 1.0
                    if nodes[subset[j]].role == "meal" and meals_in_tour < meals_required:
                        meal_boost = 3.5

                    theme_boost = 1.0
                    node_theme = _get_primary_theme(nodes[subset[j]])
                    if node_theme:
                        if user_themes:
                            if node_theme in user_themes:
                                theme_count = theme_counts_tour.get(node_theme, 0)
                                if theme_count == 0:
                                    theme_boost = 2.5
                                elif theme_count < max(theme_counts_tour.values(), default=1):
                                    theme_boost = 1.5
                        else:
                            if node_theme not in themes_in_tour:
                                theme_boost = 1.8

                    val = tau * eta * meal_boost * theme_boost
                    probs.append((j, val))
                    denom += val

                if denom == 0.0:
                    next_node = random.choice(remaining)
                else:
                    r = random.random() * denom
                    acc = 0.0
                    next_node = remaining[-1]
                    for j, val in probs:
                        acc += val
                        if acc >= r:
                            next_node = j
                            break

                tour_local.append(next_node)
                remaining.remove(next_node)

                if nodes[subset[next_node]].role == "meal":
                    meals_in_tour += 1

                next_theme = _get_primary_theme(nodes[subset[next_node]])
                if next_theme:
                    themes_in_tour.add(next_theme)
                    theme_counts_tour[next_theme] = theme_counts_tour.get(next_theme, 0) + 1

                current = next_node

            day_order_indices = [subset[k] for k in tour_local]
            cost, dist_eq, stops, visited_ids, meals = _simulate_day_route(
                day=day,
                nodes=nodes,
                travel=travel,
                order=day_order_indices,
                day_index=day_index,
                meals_min=meals_required,
                mandatory_for_day=mandatory_for_day,
                cfg=cfg,
                user_themes=user_themes,
            )
            solutions.append((cost, tour_local, dist_eq, visited_ids, meals))

        for i in range(m):
            for j in range(m):
                pheromone[i][j] *= 1.0 - cfg.acs_evaporation_rate

        solutions.sort(key=lambda x: x[0])
        elite = solutions[: max(1, m // 2)]

        for cost, tour_local, _, _, _ in elite:
            if math.isinf(cost):
                continue
            deposit = cfg.acs_q / cost if cost > 0 else 0.0
            for i in range(len(tour_local) - 1):
                a, b = tour_local[i], tour_local[i + 1]
                pheromone[a][b] += deposit
                pheromone[b][a] += deposit

        if elite and elite[0][0] < best_cost:
            best_cost = elite[0][0]
            best_order = [subset[k] for k in elite[0][1]]
            no_improvement_count = 0
        else:
            no_improvement_count += 1
            if no_improvement_count >= max_no_improvement and iteration >= 15:
                break

    if not best_order:
        depot = nodes[0]
        return DayRoute(
            date=day.date.isoformat(),
            stops=[
                {
                    "poi_id": depot.poi_id,
                    "name": depot.name,
                    "role": ROLE_ACCOMMODATION,
                    "arrival": format_time_minutes(day.start_min),
                    "start_service": format_time_minutes(day.start_min),
                    "depart": format_time_minutes(day.start_min),
                    "latitude": depot.lat,
                    "longitude": depot.lon,
                }
            ],
            meals=0,
            total_cost=float("inf"),
            total_distance=0.0,
            visited_base_ids=set(),
            infeasible=True,
        )

    best_order = _two_opt_improve(
        order=best_order,
        nodes=nodes,
        travel=travel,
        day=day,
        day_index=day_index,
        meals_min=meals_required,
        mandatory_for_day=mandatory_for_day,
        cfg=cfg,
        user_themes=user_themes,
    )

    cost, dist_eq, stops, visited_ids, meals = _simulate_day_route(
        day=day,
        nodes=nodes,
        travel=travel,
        order=best_order,
        day_index=day_index,
        meals_min=meals_required,
        mandatory_for_day=mandatory_for_day,
        cfg=cfg,
        user_themes=user_themes,
    )

    return DayRoute(
        date=day.date.isoformat(),
        stops=stops,
        meals=meals,
        total_cost=cost,
        total_distance=dist_eq,
        visited_base_ids=visited_ids,
    )


def run_acs_cvrptw(
    day_specs: List[DaySpec],
    nodes: List[Node],
    travel: List[List[int]],
    meals_required: int = 3,
    mandatory: Optional[Dict[str, Dict]] = None,
    cfg: VRPConfig = vrp_config,
    user_themes: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Run the ACS-based CVRPTW solver for a multi-day itinerary.

    This solver uses an Ant Colony System algorithm to find near-optimal routes
    for each day, considering time windows, service times, meal requirements,
    and theme balance constraints (balancing among user-selected themes).

    Args:
        user_themes: Set of theme names the user selected. If provided, the solver
                    will try to balance coverage across these themes. If None/empty,
                    it will diversify across all available themes.
    """
    if not day_specs or len(nodes) <= 1:
        return {"days": [], "meta": {"note": "No days or POIs to process."}}

    mandatory_base_ids: Set[str] = {_get_base_id(n.poi_id) for n in nodes if n.is_mandatory}

    visited_global: Set[str] = set()
    result_days: List[Dict] = []
    total_distance = 0.0
    meta: Dict[str, Any] = {}

    day_order = []
    for day in day_specs:
        day_idx = day.day_index
        feasible = 0
        feasible_attractions = 0
        mandatory_cnt = 0
        for idx, n in enumerate(nodes):
            if idx == 0:
                continue
            if day_idx not in n.windows_by_day:
                continue
            feasible += 1
            if n.role == "attraction":
                feasible_attractions += 1
            if n.is_mandatory:
                mandatory_cnt += 1
        day_order.append((-mandatory_cnt, feasible, day.day_index))

    ordered_day_specs = sorted(day_specs, key=lambda d: next(x for x in day_order if x[2] == d.day_index))

    results_by_day_index: Dict[int, Dict[str, Any]] = {}

    for day in ordered_day_specs:
        day_index = day.day_index

        candidates: List[int] = []
        mandatory_for_day: Set[str] = set()

        for idx, n in enumerate(nodes):
            if idx == 0:
                continue

            base = _get_base_id(n.poi_id)
            if base in visited_global:
                continue
            if day_index not in n.windows_by_day:
                continue

            candidates.append(idx)
            if n.is_mandatory:
                mandatory_for_day.add(base)

        available_meals = sum(1 for i in candidates if nodes[i].role == "meal")
        meals_min = min(meals_required, available_meals)

        day_route = _acs_optimize_day(
            day=day,
            nodes=nodes,
            travel=travel,
            day_index=day_index,
            available_node_indices=candidates,
            meals_required=meals_min,
            mandatory_for_day=mandatory_for_day,
            cfg=cfg,
            user_themes=user_themes,
        )

        if day_route.infeasible:
            meta.setdefault("infeasible_days", []).append(day_route.date)

        visited_global.update(day_route.visited_base_ids)
        total_distance += day_route.total_distance

        results_by_day_index[day_index] = {
            "date": day_route.date,
            "stops": day_route.stops,
            "meals": day_route.meals,
        }

    for day in day_specs:
        result_days.append(
            results_by_day_index.get(day.day_index, {"date": day.date.isoformat(), "stops": [], "meals": []})
        )

    missed_mandatory = mandatory_base_ids - visited_global
    meta.update(
        {
            "total_distance": round(total_distance, 2),
            "total_stops": sum(len(d.get("stops", [])) for d in result_days),
        }
    )
    if missed_mandatory:
        meta["missed_mandatory"] = list(missed_mandatory)

    return {
        "days": result_days,
        "meta": meta,
    }
