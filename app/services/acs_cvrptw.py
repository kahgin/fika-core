"""
ACS-based solver for CVRPTW (Capacitated Vehicle Routing Problem with Time Windows).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Set, Any

from app.services.osrm import tiered_round
from app.services.vrp_model import VRPConfig, DaySpec, Node, vrp_config
from app.services.vrp_utils import format_time_minutes
from app.utils.logger import get_logger

logger = get_logger(__name__)


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
    """Strip internal suffixes to get base POI ID."""
    return poi_id.rsplit("_", 1)[0]


def _get_primary_theme(node: Node) -> Optional[str]:
    """Return the primary theme for a node."""
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

    Hotel events (check-in/check-out) are handled as FIXED anchor points:
    - Check-out: scheduled at day start (10:00-12:00 window)
    - Check-in: scheduled in afternoon (14:00-16:00 window)
    - Regular POIs fill the gaps around these anchors
    """
    depot_idx = 0
    t = day.start_min
    current = None

    stops: List[Dict] = []
    visited_base_ids: Set[str] = set()
    visited_hotel_events: Set[str] = set()
    meals_count = 0
    meals_in_window = 0
    food_streak = 0
    total_travel_min = 0
    last_role: str = ROLE_DEPOT_INTERNAL
    theme_count_per_day: Dict[str, int] = {}
    extra_penalty_total = 0.0

    SOFT_TOL = 30

    # Separate hotel events from regular POIs
    checkout_nodes = []
    checkin_nodes = []
    stay_nodes = []
    regular_nodes = []

    for node_idx in order:
        n = nodes[node_idx]
        if n.hotel_event_type == "checkout":
            checkout_nodes.append(node_idx)
        elif n.hotel_event_type == "checkin":
            checkin_nodes.append(node_idx)
        elif n.hotel_event_type == "stay":
            stay_nodes.append(node_idx)
        else:
            regular_nodes.append(node_idx)

    # Helper to add a stop
    def add_stop(node_idx: int, arrival: int, start_service: int, finish_service: int) -> bool:
        nonlocal t, current, total_travel_min, meals_count, meals_in_window, food_streak, last_role

        n = nodes[node_idx]
        clean_poi_id = _get_base_id(n.poi_id)

        stop_dict = {
            "poi_id": clean_poi_id,
            "name": n.name,
            "role": n.role,
            "themes": n.themes or [],
            "arrival": format_time_minutes(arrival),
            "start_service": format_time_minutes(start_service),
            "depart": format_time_minutes(finish_service),
            "latitude": n.lat,
            "longitude": n.lon,
        }
        if n.images:
            stop_dict["images"] = n.images
        if n.hotel_event_type:
            stop_dict["hotel_event_type"] = n.hotel_event_type
        stops.append(stop_dict)

        t = finish_service
        current = node_idx

        if n.role != "accommodation":
            visited_base_ids.add(clean_poi_id)
        else:
            visited_hotel_events.add(n.poi_id)

        if n.role == "meal":
            meals_count += 1
            food_streak += 1
        else:
            food_streak = 0
        last_role = n.role
        return True

    # PHASE 0: Add STAY marker at start of day (informational, no time consumed)
    for node_idx in stay_nodes:
        n = nodes[node_idx]
        if day_index not in n.windows_by_day:
            continue
        add_stop(node_idx, day.start_min, day.start_min, day.start_min)

    # Get checkout window info for scheduling POIs before/after
    checkout_window_start = None
    checkout_window_end = None
    checkout_node_idx = None
    if checkout_nodes:
        checkout_node_idx = checkout_nodes[0]
        n = nodes[checkout_node_idx]
        if day_index in n.windows_by_day:
            checkout_window_start, checkout_window_end = n.windows_by_day[day_index][0]

    # PHASE 1a: Schedule POIs BEFORE checkout window opens (if there's time)
    if checkout_window_start is not None and t < checkout_window_start:
        for node_idx in regular_nodes:
            n = nodes[node_idx]
            if day_index not in n.windows_by_day:
                continue

            if n.role == "meal" and meals_count >= 3:
                continue
            if n.role == "meal" and food_streak >= 2:
                continue
            if n.role == "meal" and last_role == "meal":
                continue

            if current is None:
                travel_min = 0
                arrival = t
            else:
                travel_min = travel[current][node_idx]
                arrival = t + travel_min

            chosen_window = None
            start_service = None
            finish_service = None

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

            # Must finish before checkout window starts (with travel time to hotel)
            travel_to_checkout = travel[node_idx][checkout_node_idx]
            if finish_service + travel_to_checkout > checkout_window_start:
                continue

            total_travel_min += travel_min
            add_stop(node_idx, max(arrival, chosen_window[0]), start_service, finish_service)

            if n.role == "attraction":
                primary_theme = _get_primary_theme(n)
                if primary_theme:
                    theme_count_per_day[primary_theme] = theme_count_per_day.get(primary_theme, 0) + 1

    # PHASE 1b: Schedule checkout
    if checkout_node_idx is not None:
        n = nodes[checkout_node_idx]
        if day_index in n.windows_by_day:
            w_start, w_end = n.windows_by_day[day_index][0]

            if current is not None:
                travel_min = travel[current][checkout_node_idx]
                arrival = t + travel_min
                total_travel_min += travel_min
            else:
                arrival = t

            start_service = max(arrival, w_start)
            finish_service = start_service + n.service

            if finish_service <= w_end:
                add_stop(checkout_node_idx, max(arrival, w_start), start_service, finish_service)

    # PHASE 2: Schedule regular POIs until check-in time
    checkin_start_time = cfg.hotel_check_in_window[0] if checkin_nodes else day.end_min

    for node_idx in regular_nodes:
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

        if current is None:
            travel_min = 0
            arrival = t
        else:
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

        # If we have a check-in, ensure we leave time for it
        if checkin_nodes:
            travel_to_checkin = travel[node_idx][checkin_nodes[0]]
            if finish_service + travel_to_checkin > checkin_start_time + 60:
                continue

        back_to_depot = travel[node_idx][depot_idx]
        if finish_service + back_to_depot > day.end_min:
            continue

        total_travel_min += travel_min
        extra_penalty_total += extra_penalty

        arrival_display = max(arrival, chosen_window[0])
        add_stop(node_idx, arrival_display, start_service, finish_service)

        if n.role == "attraction" and primary_theme:
            theme_count_per_day[primary_theme] = theme_count_per_day.get(primary_theme, 0) + 1

    # PHASE 3: Schedule check-in (if any)
    for node_idx in checkin_nodes:
        n = nodes[node_idx]
        if day_index not in n.windows_by_day:
            continue

        w_start, w_end = n.windows_by_day[day_index][0]

        if current is not None:
            travel_min = travel[current][node_idx]
            arrival = t + travel_min
            total_travel_min += travel_min
        else:
            arrival = t

        # Wait until check-in window opens
        start_service = max(arrival, w_start)
        finish_service = start_service + n.service

        if finish_service <= w_end:
            add_stop(node_idx, max(arrival, w_start), start_service, finish_service)

    # PHASE 4: Schedule remaining POIs after check-in (if time permits)
    scheduled_indices = set(s.get("poi_id") for s in stops)
    for node_idx in regular_nodes:
        n = nodes[node_idx]
        clean_poi_id = _get_base_id(n.poi_id)

        if clean_poi_id in scheduled_indices:
            continue
        if clean_poi_id in visited_base_ids:
            continue
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

        if current is None:
            travel_min = 0
            arrival = t
        else:
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

        total_travel_min += travel_min
        extra_penalty_total += extra_penalty

        arrival_display = max(arrival, chosen_window[0])
        add_stop(node_idx, arrival_display, start_service, finish_service)

        if n.role == "attraction" and primary_theme:
            theme_count_per_day[primary_theme] = theme_count_per_day.get(primary_theme, 0) + 1

        if meal_is_in_window:
            meals_in_window += 1

    # Calculate return to depot
    if current is None:
        back_min = 0
    else:
        back_min = travel[current][depot_idx]

    if t + back_min > day.end_min:
        return float("inf"), 0.0, [], set(), 0

    total_travel_min += back_min

    # Calculate cost
    cost = float(total_travel_min) + extra_penalty_total

    poi_count = len(visited_base_ids)
    cost -= cfg.poi_visit_bonus * poi_count

    if stops:
        first_start = int(stops[0]["arrival"].split(":")[0]) * 60 + int(stops[0]["arrival"].split(":")[1])
        last_end = int(stops[-1]["depart"].split(":")[0]) * 60 + int(stops[-1]["depart"].split(":")[1])
        active_time = last_end - first_start
        day_budget = day.end_min - day.start_min
        utilization = active_time / day_budget if day_budget > 0 else 1.0
        # Stronger utilization penalty: target 80% utilization
        if utilization < 0.8:
            cost += (0.8 - utilization) * 300

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
        missed_count = 0
        for m_id in mandatory_for_day:
            if "_checkin" in m_id or "_checkout" in m_id:
                if m_id not in visited_hotel_events:
                    missed_count += 1
            else:
                base_id = _get_base_id(m_id)
                if base_id not in visited_base_ids:
                    missed_count += 1
        if missed_count > 0:
            cost += cfg.mandatory_miss_penalty * missed_count

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
    """Run the Ant Colony System optimization for a single day."""
    if not available_node_indices:
        return DayRoute(
            date=day.date.isoformat(),
            stops=[],
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

                    # Meal boost: ensure 3 meals per day when possible
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
                                    theme_boost = 2.0
                                elif theme_count < max(theme_counts_tour.values(), default=1):
                                    theme_boost = 1.3
                        else:
                            if node_theme not in themes_in_tour:
                                theme_boost = 1.5

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
        return DayRoute(
            date=day.date.isoformat(),
            stops=[],
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
    """Run the ACS-based CVRPTW solver for a multi-day itinerary."""
    if not day_specs or len(nodes) <= 1:
        return {"days": [], "meta": {"note": "No days or POIs to process."}}

    # Track BASE IDs of mandatory POIs
    mandatory_base_ids: Set[str] = {_get_base_id(n.poi_id) for n in nodes if n.is_mandatory}

    # Track visited BASE IDs globally to prevent duplicates
    visited_global: Set[str] = set()
    result_days: List[Dict] = []
    total_distance = 0.0
    meta: Dict[str, Any] = {}

    day_order = []
    for day in day_specs:
        day_idx = day.day_index
        feasible = 0
        mandatory_cnt = 0
        for idx, n in enumerate(nodes):
            if idx == 0:
                continue
            if day_idx not in n.windows_by_day:
                continue
            feasible += 1
            if n.is_mandatory:
                mandatory_cnt += 1
        day_order.append((-mandatory_cnt, feasible, day.day_index))

    ordered_day_specs = sorted(day_specs, key=lambda d: next(x for x in day_order if x[2] == d.day_index))

    results_by_day_index: Dict[int, Dict[str, Any]] = {}

    for day in ordered_day_specs:
        day_index = day.day_index

        candidates: List[int] = []
        mandatory_for_day: Set[str] = set()
        has_all_day_poi = False
        all_day_node_idx: Optional[int] = None

        # First pass: check if there's an all-day mandatory POI for this day
        for idx, n in enumerate(nodes):
            if idx == 0:
                continue
            if day_index not in n.windows_by_day:
                continue
            if n.is_all_day and n.is_mandatory:
                has_all_day_poi = True
                all_day_node_idx = idx
                break

        for idx, n in enumerate(nodes):
            if idx == 0:
                continue

            # Check BASE ID against visited set (skip for accommodation - they have multiple events)
            base_id = _get_base_id(n.poi_id)
            if n.role != "accommodation" and base_id in visited_global:
                continue
            if day_index not in n.windows_by_day:
                continue

            # If there's an all-day POI, only include hotel events and the all-day POI itself
            if has_all_day_poi:
                if n.role == "accommodation" or idx == all_day_node_idx:
                    candidates.append(idx)
                    if n.is_mandatory:
                        mandatory_for_day.add(n.poi_id)
                continue

            candidates.append(idx)
            if n.is_mandatory:
                mandatory_for_day.add(n.poi_id)

        available_meals = sum(1 for i in candidates if nodes[i].role == "meal")
        meals_min = 0 if has_all_day_poi else min(meals_required, available_meals)

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

        # Update global visited with BASE IDs
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
