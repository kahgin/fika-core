from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Set

from app.services.cvrptw import DaySpec, Node, LUNCH_WIN, DINNER_WIN

# Penalties
MEAL_SHORTFALL_PENALTY = 60 * 10  # 10 hours (in "minute-cost" units) per missing meal
MANDATORY_MISS_PENALTY = 60 * 24 * 7  # 7 days of cost per missed mandatory POI
DROP_PENALTY_BASE = 2000  # (kept for future use, not used directly here)


@dataclass(slots=True)
class ACSConfig:
    """Configuration for ACS-based CVRPTW solver."""

    n_ants: int = 30
    n_iterations: int = 60
    alpha: float = 1.0  # pheromone importance
    beta: float = 2.0  # heuristic importance
    evaporation_rate: float = 0.5
    q: float = 100.0  # pheromone deposit factor
    seed: Optional[int] = None  # for reproducibility


@dataclass(slots=True)
class DayRoute:
    """Concrete schedule for one day."""

    date: str
    stops: List[Dict]
    meals: int
    total_cost: float
    total_distance: float
    visited_base_ids: Set[str]


def _fmt_time(t: int) -> str:
    """Minutes from midnight -> 'HH:MM'."""
    h = t // 60
    m = t % 60
    return f"{h:02d}:{m:02d}"


def _base_id(poi_id: str) -> str:
    """Strip '_dayX' suffix to get logical POI id."""
    if "_day" in poi_id:
        return poi_id.rsplit("_day", 1)[0]
    return poi_id


def _meal_in_preferred_window(start_min: int) -> bool:
    """Check if a meal start time is within lunch or dinner windows."""
    for win in (LUNCH_WIN, DINNER_WIN):
        if win[0] <= start_min <= win[1]:
            return True
    return False


def _primary_theme(node: Node) -> Optional[str]:
    """
    Primary theme used for theme-repetition rules.
    If themes list exists, use the first; otherwise fall back to role.
    """
    if node.themes:
        return node.themes[0]
    return node.role


def _simulate_day_route(
    day: DaySpec,
    nodes: List[Node],
    travel: List[List[int]],
    order: List[int],  # sequence of node indices (excluding depot)
    day_index: int,
    meals_min: int,
    meals_max: int,
    mandatory_for_day: Set[str],
) -> Tuple[float, float, List[Dict], Set[str], int]:
    """
    Given a permutation of POI node indices, build a feasible schedule for that day.

    Enforced inside this simulator:
    - Day start/end horizon (no overrun).
    - Time windows per day (windows_by_day[day_index]).
    - Service times.
    - Ability to return to depot before day end.
    - Hard cap on meals per day (meals_max).
    - No consecutive meals.
    - No 3-poi runs with the same primary theme.
    - Mealtime appropriateness (hard/soft rules via meal windows).
    - Per-day penalty if mandatory POIs for this day are not visited.

    Returns:
        (cost, distance_km_equiv, stops, visited_base_ids, meals_count)

    If infeasible, returns (inf, 0, [], empty_set, 0).
    """
    depot_idx = 0  # by construction in cvrptw
    t = day.start_min
    current = depot_idx
    stops: List[Dict] = []
    visited_base_ids: Set[str] = set()
    meals_count = 0
    food_streak = 0
    total_travel_min = 0
    last_role: str = "depot"
    last_themes: List[Optional[str]] = []
    extra_penalty_total = 0.0

    # Meal-window config (independent of LUNCH/DINNER preference logic)
    breakfast = (7 * 60, 10 * 60)
    lunch = (12 * 60, 14 * 60)
    dinner = (18 * 60, 21 * 60)
    MEAL_WINDOWS = [breakfast, lunch, dinner]
    SOFT_TOL = 30  # allow ±30 min
    HARD_TOL = 90  # max allowed deviation

    for node_idx in order:
        n = nodes[node_idx]

        # Skip nodes that are not available on this day
        if day_index not in n.windows_by_day:
            continue

        primary_theme = _primary_theme(n)

        # Hard constraint: no 3 same themes in a row
        if (
            primary_theme is not None
            and len(last_themes) >= 2
            and last_themes[-1] == primary_theme
            and last_themes[-2] == primary_theme
        ):
            # Would create theme A-A-A
            continue

        # Hard cap on meals per day
        if n.role == "meal" and meals_count >= meals_max:
            continue
        
        # Identify food-like stops: meal OR POI with theme food_culinary
        is_food_theme = bool(n.themes and "food_culinary" in n.themes)
        is_food_like = (n.role == "meal") or is_food_theme

        # Forbid 3 consecutive food-like stops
        if is_food_like and food_streak >= 2:
            continue

        # Hard constraint: no consecutive meals
        if n.role == "meal" and last_role == "meal":
            continue

        # Travel to node
        travel_min = travel[current][node_idx]
        arrival = t + travel_min

        # Strict meal-time rules (relative to estimated arrival)
        extra_penalty = 0.0
        if n.role == "meal":
            arrival_est = arrival

            deltas = []
            for start, end in MEAL_WINDOWS:
                if start <= arrival_est <= end:
                    deltas.append(0)
                elif arrival_est < start:
                    deltas.append(start - arrival_est)
                else:
                    deltas.append(arrival_est - end)
            best_delta = min(deltas) if deltas else HARD_TOL + 1

            # Too far from any meal window → disallow entirely
            if best_delta > HARD_TOL:
                continue

            # Slightly outside (soft tolerance) → cost penalty
            if best_delta > SOFT_TOL:
                extra_penalty = 30.0 * float(best_delta - SOFT_TOL)

        # Find a usable time window for this day
        windows = n.windows_by_day[day_index]
        chosen_window: Optional[Tuple[int, int]] = None
        start_service: Optional[int] = None
        finish_service: Optional[int] = None

        for w_start, w_end in windows:
            if arrival > w_end:
                continue  # too late for this window
            start_service = max(arrival, w_start)
            finish_service = start_service + n.service
            if finish_service <= w_end:
                chosen_window = (w_start, w_end)
                break

        if not chosen_window or start_service is None or finish_service is None:
            # Can't fit in any window; skip this node
            continue

        # Meal-time preference after we know actual service start
        if n.role == "meal":
            in_pref = _meal_in_preferred_window(start_service)
            if meals_count >= meals_min and not in_pref:
                # This is an extra meal at a weird time -> skip
                continue

        # Ensure we can still return to depot by day end after visiting
        back_to_depot = travel[node_idx][depot_idx]
        if finish_service + back_to_depot > day.end_min:
            # Visiting this node would violate day horizon
            continue

        # Accept this visit
        t = finish_service
        total_travel_min += travel_min
        current = node_idx
        extra_penalty_total += extra_penalty

        # Display arrival not earlier than opening time (to match hours in validator)
        arrival_display = max(arrival, chosen_window[0])

        stops.append(
            {
                "poi_id": n.poi_id,
                "name": n.name,
                "role": n.role,
                "themes": n.themes if n.role == "attraction" else [],
                "arrival": _fmt_time(arrival_display),
                "start_service": _fmt_time(start_service),
                "depart": _fmt_time(finish_service),
            }
        )

        if is_food_like:
            food_streak += 1
        else:
            food_streak = 0

        visited_base_ids.add(_base_id(n.poi_id))
        if n.role == "meal":
            meals_count += 1
        last_role = n.role

        if primary_theme is not None:
            last_themes.append(primary_theme)
            if len(last_themes) > 2:
                last_themes.pop(0)

    # After visiting, return to depot
    back_min = travel[current][depot_idx]
    if t + back_min > day.end_min:
        # Route infeasible
        return float("inf"), 0.0, [], set(), 0

    total_travel_min += back_min

    # Add final depot/hotel return
    depot_node = nodes[depot_idx]
    arrival_back = t + back_min
    stops.append(
        {
            "poi_id": depot_node.poi_id,
            "name": depot_node.name,
            "role": depot_node.role,
            "themes": [],
            "arrival": _fmt_time(arrival_back),
            "start_service": _fmt_time(arrival_back),
            "depart": _fmt_time(arrival_back),
            "latitude": depot_node.lat,
            "longitude": depot_node.lon,
        }
    )

    # Base cost: travel time
    cost = float(total_travel_min) + extra_penalty_total

    # Meal quota penalties (per day)
    if meals_count < meals_min:
        missing = meals_min - meals_count
        cost += MEAL_SHORTFALL_PENALTY * missing

    # Mandatory POIs for this day: strong penalty if not visited
    if mandatory_for_day:
        visited_mandatory = visited_base_ids & mandatory_for_day
        missed_mandatory = mandatory_for_day - visited_mandatory
        if missed_mandatory:
            cost += MANDATORY_MISS_PENALTY * len(missed_mandatory)

    # distance in "km equivalent" (here: hours as proxy)
    distance_km_equiv = total_travel_min / 60.0

    return cost, distance_km_equiv, stops, visited_base_ids, meals_count


def _acs_optimize_day(
    day: DaySpec,
    nodes: List[Node],
    travel: List[List[int]],
    day_index: int,
    available_node_indices: List[int],
    meals_required: int,
    mandatory_for_day: Set[str],
    cfg: ACSConfig,
) -> DayRoute:
    """
    Run ACS for a single day on the given subset of node indices.
    Returns the best feasible DayRoute found (w.r.t. cost).
    """
    if cfg.seed is not None:
        random.seed(cfg.seed)

    # If nothing to visit, just stay at hotel
    if not available_node_indices:
        depot = nodes[0]
        return DayRoute(
            date=day.date.isoformat(),
            stops=[
                {
                    "poi_id": depot.poi_id,
                    "name": depot.name,
                    "role": "hotel",
                    "arrival": _fmt_time(day.start_min),
                    "start_service": _fmt_time(day.start_min),
                    "depart": _fmt_time(day.start_min),
                    "latitude": depot.lat,
                    "longitude": depot.lon,
                }
            ],
            meals=0,
            total_cost=0.0,
            total_distance=0.0,
            visited_base_ids=set(),
        )

    # Build a local index [0..M-1] for ACS over the subset
    subset = available_node_indices
    m = len(subset)

    # Distances between subset nodes (we ignore depot here;
    # schedule simulation includes depot legs)
    distances = [[0.0] * m for _ in range(m)]
    for i in range(m):
        ni = subset[i]
        for j in range(m):
            nj = subset[j]
            distances[i][j] = float(travel[ni][nj])

    # Initialize pheromones and heuristic
    pheromone = [[1.0 for _ in range(m)] for _ in range(m)]
    heuristic = [[0.0 for _ in range(m)] for _ in range(m)]
    for i in range(m):
        for j in range(m):
            d = distances[i][j]
            heuristic[i][j] = 1.0 / d if d > 0 else 0.0

    best_cost = float("inf")
    best_order: List[int] = []

    for _ in range(cfg.n_iterations):
        solutions: List[Tuple[float, List[int], float, Set[str], int]] = []

        for _ in range(cfg.n_ants):
            # Construct a permutation over [0..m-1]
            remaining = list(range(m))
            current = random.choice(remaining)
            tour_local = [current]
            remaining.remove(current)

            while remaining:
                probs: List[Tuple[int, float]] = []
                denom = 0.0
                for j in remaining:
                    tau = pheromone[current][j] ** cfg.alpha
                    eta = heuristic[current][j] ** cfg.beta
                    val = tau * eta
                    probs.append((j, val))
                    denom += val

                if denom == 0.0:
                    next_local = random.choice(remaining)
                else:
                    r = random.random() * denom
                    acc = 0.0
                    next_local = remaining[-1]
                    for j, val in probs:
                        acc += val
                        if acc >= r:
                            next_local = j
                            break

                tour_local.append(next_local)
                remaining.remove(next_local)
                current = next_local

            # Map local indices -> global node indices
            day_order_indices = [subset[k] for k in tour_local]

            # Evaluate route with constraints
            cost, dist_eq, stops, visited_ids, meals = _simulate_day_route(
                day=day,
                nodes=nodes,
                travel=travel,
                order=day_order_indices,
                day_index=day_index,
                meals_min=meals_required,
                meals_max=3,
                mandatory_for_day=mandatory_for_day,
            )

            solutions.append((cost, tour_local, dist_eq, visited_ids, meals))

        # Evaporation
        for i in range(m):
            for j in range(m):
                pheromone[i][j] *= 1.0 - cfg.evaporation_rate

        # Deposit pheromone from best ants (by cost)
        solutions.sort(key=lambda x: x[0])
        elite = solutions[: max(1, m // 2)]

        for cost, tour_local, _, _, _ in elite:
            if math.isinf(cost):
                continue
            deposit = cfg.q / cost if cost > 0 else 0.0
            for i in range(len(tour_local) - 1):
                a = tour_local[i]
                b = tour_local[i + 1]
                pheromone[a][b] += deposit
                pheromone[b][a] += deposit

        # Track global best
        for cost, tour_local, _, _, _ in elite:
            if cost < best_cost:
                best_cost = cost
                best_order = [subset[k] for k in tour_local]

    # Build final schedule from best_order
    if not best_order:
        # Fallback: no feasible route found; return hotel-only day
        depot = nodes[0]
        return DayRoute(
            date=day.date.isoformat(),
            stops=[
                {
                    "poi_id": depot.poi_id,
                    "name": depot.name,
                    "role": "hotel",
                    "arrival": _fmt_time(day.start_min),
                    "start_service": _fmt_time(day.start_min),
                    "depart": _fmt_time(day.start_min),
                    "latitude": depot.lat,
                    "longitude": depot.lon,
                }
            ],
            meals=0,
            total_cost=float("inf"),
            total_distance=0.0,
            visited_base_ids=set(),
        )

    cost, dist_eq, stops, visited_ids, meals = _simulate_day_route(
        day=day,
        nodes=nodes,
        travel=travel,
        order=best_order,
        day_index=day_index,
        meals_min=meals_required,
        meals_max=3,
        mandatory_for_day=mandatory_for_day,
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
    cfg: Optional[ACSConfig] = None,
) -> Dict:
    """
    ACS-based CVRPTW solver (multi-day) that:

    - Respects day horizons & pacing (start/end).
    - Uses per-day time windows derived from POI opening hours.
    - Respects service times.
    - Visits each base POI at most once across the entire trip.
    - Enforces daily meal requirements (soft, with heavy penalties).
    - Enforces no consecutive meals.
    - Forbids 3 POIs in a row with the same primary theme.
    - Prefers meals in lunch/dinner windows.
    - Applies heavy penalties if mandatory POIs for a given day are not visited.
    """
    if cfg is None:
        cfg = ACSConfig()

    if not day_specs or len(nodes) <= 1:
        return {"days": [], "meta": {"note": "No days or POIs"}}

    # Global mandatory base IDs (from nodes flagged as is_mandatory)
    mandatory_base_ids: Set[str] = {
        _base_id(n.poi_id) for n in nodes if getattr(n, "is_mandatory", False)
    }

    visited_global: Set[str] = set()
    result_days: List[Dict] = []
    total_distance = 0.0

    for day in day_specs:
        day_index = day.day_index

        # Candidates: nodes that are available on this day and not yet visited globally
        candidates: List[int] = []
        mandatory_for_day: Set[str] = set()

        for idx, n in enumerate(nodes):
            if idx == 0:
                continue  # depot
            base = _base_id(n.poi_id)
            if base in visited_global:
                continue
            if day_index not in n.windows_by_day:
                continue
            candidates.append(idx)
            if getattr(n, "is_mandatory", False):
                mandatory_for_day.add(base)

        # Adjust meals_required based on how many meals are actually available this day
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
        )

        # Update global visited
        visited_global.update(day_route.visited_base_ids)
        total_distance += day_route.total_distance

        result_days.append(
            {
                "date": day_route.date,
                "stops": day_route.stops,
                "meals": day_route.meals,
            }
        )

    # Mandatory POI meta: which mandatory POIs were missed globally
    missed_mandatory = mandatory_base_ids - visited_global
    meta = {
        "total_distance": round(total_distance, 2),
        "total_stops": sum(len(d["stops"]) for d in result_days),
    }
    if missed_mandatory:
        meta["missed_mandatory"] = list(missed_mandatory)
        meta["note"] = (
            f"ACS solution missed {len(missed_mandatory)} mandatory POIs; "
            "consider relaxing other constraints or checking time windows."
        )

    return {
        "days": result_days,
        "meta": meta,
    }
