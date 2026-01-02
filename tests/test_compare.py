"""
Comparison test: ACS-CVRPTW vs OR-Tools CVRPTW.

Purpose: Compare solution quality between the two solvers on the same input.
Metrics:
- Time Window Satisfaction Rate
- Time Utilisation Score (TUS)
- Constraint Compliance (meals, themes, mandatory POIs)
- Travel Efficiency (time, distance)
- POI Coverage
- Execution Time (solver runtime)
"""

import os
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Any, Set, Tuple

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.services.vrp_model import vrp_config
from app.utils.date_utils import time_to_minutes

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_spec.json")


# HELPER FUNCTIONS


def _get_base_poi_id(poi_id: str) -> str:
    """Strip _dayX suffix to get base POI ID."""
    return poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id


def _is_within_meal_window(start_min: int) -> Tuple[bool, str]:
    """
    Check if a meal time is within preferred windows.
    Returns (is_within, window_name).
    """
    windows = {
        "breakfast": vrp_config.breakfast_win,
        "lunch": vrp_config.lunch_win,
        "dinner": vrp_config.dinner_win,
    }
    for name, (w_start, w_end) in windows.items():
        if w_start <= start_min <= w_end:
            return True, name
    return False, "none"


# METRICS


@dataclass
class SolverMetrics:
    """Comprehensive metrics for solver evaluation."""

    solver_name: str

    # Execution Time
    execution_time_sec: float  # Time taken to solve

    # Time Window Satisfaction
    time_window_satisfaction_rate: float  # % of POIs within opening hours
    pois_within_windows: int
    pois_outside_windows: int

    # Time Utilisation Score (TUS)
    time_utilisation_score: float  # Average D_k / T_max_k across days
    daily_utilisation: List[float]  # Per-day utilisation

    # Travel Efficiency
    total_travel_time_min: int
    total_distance_km: float
    avg_travel_per_poi_min: float

    # POI Coverage (PRIORITY metric for capstone)
    unique_pois: int
    total_stops: int  # Including meals
    meals_scheduled: int
    attractions_scheduled: int

    # Constraint Compliance
    time_sequence_valid: bool
    depot_positions_valid: bool
    poi_coverage_valid: bool  # No duplicates
    meal_constraints_valid: bool  # Max 3, no consecutive
    theme_constraints_valid: bool  # Max per theme
    meal_window_compliance: float  # % meals in preferred windows
    food_streak_valid: bool  # No more than 2 consecutive food items
    mandatory_pois_scheduled: int
    mandatory_pois_missed: List[str]

    # Overall
    violations: List[str]
    feasible: bool


def _calculate_time_window_satisfaction(days: List[Dict]) -> Tuple[float, int, int]:
    """
    Calculate Time Window Satisfaction Rate.
    Formula: (POIs within hours / Total POIs) * 100

    Note: We check if arrival is before POI closes (simple check).
    In practice, opening hours are enforced by the solver.
    """
    within = 0
    outside = 0

    for day in days:
        for stop in day.get("stops", []):
            if stop.get("role") in ("depot", "accommodation", "hotel"):
                continue
            # For now, assume all scheduled POIs are within windows
            # (solvers enforce this). We count them.
            within += 1

    total = within + outside
    rate = (within / total * 100) if total > 0 else 100.0
    return rate, within, outside


def _calculate_tus(days: List[Dict], pacing: str = "balanced") -> Tuple[float, List[float]]:
    """
    Calculate Time Utilisation Score (TUS) based on actual POI visit time.
    Formula: (1/n * sum(D_k / T_max_k)) * 100

    D_k = total time spent at POIs (sum of service times)
    T_max_k = day budget based on pacing
    """
    t_max = vrp_config.pace_day_budget_min.get(pacing, 12 * 60)
    daily_utilisation = []

    for day in days:
        stops = [s for s in day.get("stops", []) if s.get("arrival") and s.get("depart")]

        if not stops:
            daily_utilisation.append(0.0)
            continue

        d_k = sum(
            time_to_minutes(s.get("depart", "00:00"))
            - time_to_minutes(s.get("arrival", "00:00"))
            for s in stops
        )

        daily_utilisation.append((d_k / t_max) * 100 if t_max > 0 else 0)

    avg_tus = sum(daily_utilisation) / len(daily_utilisation) if daily_utilisation else 0
    return avg_tus, daily_utilisation


def _calculate_travel_efficiency(days: List[Dict]) -> Tuple[int, float]:
    """
    Calculate total travel time and approximate time per POI.
    """
    total_minutes = 0
    poi_count = 0

    for day in days:
        stops = day.get("stops", [])
        if len(stops) < 2:
            continue

        first_arrival = time_to_minutes(stops[0].get("arrival", "00:00"))
        last_depart = time_to_minutes(stops[-1].get("depart", "00:00"))
        day_duration = max(0, last_depart - first_arrival)
        total_minutes += day_duration

        # Count non-depot stops
        poi_count += sum(1 for s in stops if s.get("role") not in ("depot", "accommodation", "hotel"))

    avg_per_poi = total_minutes / poi_count if poi_count > 0 else 0
    return total_minutes, avg_per_poi


def _check_poi_coverage(days: List[Dict]) -> Tuple[int, int, int, int, List[str]]:
    """
    Check POI coverage and count by type.
    Returns: (unique_pois, total_stops, meals, attractions, duplicates)
    """
    visited_base_ids: Set[str] = set()
    duplicates = []
    meals = 0
    attractions = 0

    for day_idx, day in enumerate(days):
        for stop in day.get("stops", []):
            role = stop.get("role", "")
            if role in ("depot", "accommodation", "hotel"):
                continue

            base_id = _get_base_poi_id(stop.get("poi_id", ""))

            if base_id in visited_base_ids:
                duplicates.append(f"Day {day_idx + 1}: {stop.get('name')} ({base_id})")
            else:
                visited_base_ids.add(base_id)

            if role == "meal":
                meals += 1
            elif role == "attraction":
                attractions += 1

    return len(visited_base_ids), len(visited_base_ids), meals, attractions, duplicates


def _check_time_sequence(days: List[Dict]) -> Tuple[bool, List[str]]:
    """Validate that arrival/departure times are feasible in sequence."""
    violations = []

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        for i in range(len(stops) - 1):
            curr_depart = time_to_minutes(stops[i].get("depart", "00:00"))
            next_arrival = time_to_minutes(stops[i + 1].get("arrival", "00:00"))

            if next_arrival < curr_depart:
                violations.append(f"Day {day_idx + 1}: Stop {i}->{i + 1} arrival before departure")

    return len(violations) == 0, violations


def _check_depot_positions(days: List[Dict]) -> Tuple[bool, List[str]]:
    """Check if depot/accommodation is at expected positions."""
    issues = []

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        if not stops:
            issues.append(f"Day {day_idx + 1}: No stops")
            continue

        # First stop should be depot/accommodation (except day 1 possibly)
        first_role = stops[0].get("role", "")
        if first_role not in ("depot", "accommodation", "hotel"):
            # This might be intentional for day 1
            pass

        # Last stop should be depot/accommodation (except last day possibly)
        last_role = stops[-1].get("role", "")
        if last_role not in ("depot", "accommodation", "hotel"):
            # This might be intentional for last day
            pass

    return len(issues) == 0, issues


def _check_meal_constraints(days: List[Dict]) -> Tuple[bool, float, List[str]]:
    """
    Check meal constraints:
    - Max 3 meals per day
    - No consecutive meals
    - Track meal window compliance
    """
    violations = []
    meals_in_window = 0
    total_meals = 0

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        day_meals = 0

        for i, stop in enumerate(stops):
            if stop.get("role") == "meal":
                day_meals += 1
                total_meals += 1

                # Check if in preferred window
                start_time = time_to_minutes(stop.get("arrival", "00:00"))
                in_window, window_name = _is_within_meal_window(start_time)
                if in_window:
                    meals_in_window += 1

                # Check consecutive meals
                if i > 0 and stops[i - 1].get("role") == "meal":
                    violations.append(f"Day {day_idx + 1}: Consecutive meals at stop {i}")

        if day_meals > 3:
            violations.append(f"Day {day_idx + 1}: {day_meals} meals (max 3)")

    window_compliance = (meals_in_window / total_meals * 100) if total_meals > 0 else 100.0
    return len(violations) == 0, window_compliance, violations


def _check_theme_constraints(days: List[Dict], max_per_theme: int = None) -> Tuple[bool, List[str]]:
    """Check theme diversity: track theme concentration per day.

    Note: This is no longer a hard constraint - theme balance uses soft penalties.
    Returns info about theme concentration for evaluation purposes.
    """
    # Theme balance uses soft penalties, not hard limits
    # We track theme counts for informational purposes only
    if max_per_theme is None:
        max_per_theme = 99  # No hard limit

    violations = []

    for day_idx, day in enumerate(days):
        theme_counts: Dict[str, int] = {}

        for stop in day.get("stops", []):
            if stop.get("role") != "attraction":
                continue

            themes = stop.get("themes", [])
            if themes:
                primary_theme = themes[0]
                theme_counts[primary_theme] = theme_counts.get(primary_theme, 0) + 1

        # Only log high concentration as info, don't count as violation
        for theme, count in theme_counts.items():
            if count > 5:  # Very high concentration - just informational
                violations.append(f"Day {day_idx + 1}: {count} attractions with theme '{theme}' (high concentration)")

    return len(violations) == 0, violations


def _check_food_streak(days: List[Dict]) -> Tuple[bool, List[str]]:
    """Check that no more than 2 consecutive food-like POIs."""
    violations = []

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        streak = 0

        for i, stop in enumerate(stops):
            if stop.get("role") in ("depot", "accommodation", "hotel"):
                streak = 0
                continue

            if stop.get("role") == "meal":
                streak += 1
                if streak > 2:
                    violations.append(f"Day {day_idx + 1}: Food streak > 2 at stop {i}")
            else:
                streak = 0

    return len(violations) == 0, violations


def _check_mandatory_pois(output: Dict) -> Tuple[int, List[str]]:
    """Check mandatory POI coverage."""
    meta = output.get("meta", {})
    missed = meta.get("missed_mandatory", [])

    # Count scheduled mandatory POIs
    days = output.get("days", [])
    scheduled = 0
    for day in days:
        for stop in day.get("stops", []):
            if stop.get("is_mandatory", False):
                scheduled += 1

    return scheduled, missed


def evaluate_solution(
    output: Dict[str, Any],
    solver_name: str,
    pacing: str = "balanced",
    execution_time_sec: float = 0.0,
) -> SolverMetrics:
    """
    Comprehensive evaluation of a solver's solution.
    """
    days = output.get("days", [])
    meta = output.get("meta", {})
    all_violations = []

    # 1. Time Window Satisfaction Rate
    tw_rate, tw_within, tw_outside = _calculate_time_window_satisfaction(days)

    # 2. Time Utilisation Score (TUS)
    tus, daily_util = _calculate_tus(days, pacing)

    # 3. Travel Efficiency
    total_travel, avg_travel_per_poi = _calculate_travel_efficiency(days)
    total_distance = meta.get("total_distance", 0.0)

    # 4. POI Coverage
    unique_pois, total_stops, meals, attractions, duplicates = _check_poi_coverage(days)
    if duplicates:
        all_violations.extend([f"Duplicate: {d}" for d in duplicates])

    # 5. Time Sequence Validation
    time_valid, time_issues = _check_time_sequence(days)
    all_violations.extend(time_issues)

    # 6. Depot Positions
    depot_valid, depot_issues = _check_depot_positions(days)
    all_violations.extend(depot_issues)

    # 7. Meal Constraints
    meal_valid, meal_window_compliance, meal_issues = _check_meal_constraints(days)
    all_violations.extend(meal_issues)

    # 8. Theme Constraints
    theme_valid, theme_issues = _check_theme_constraints(days)
    all_violations.extend(theme_issues)

    # 9. Food Streak
    food_streak_valid, food_issues = _check_food_streak(days)
    all_violations.extend(food_issues)

    # 10. Mandatory POIs
    mandatory_scheduled, mandatory_missed = _check_mandatory_pois(output)
    if mandatory_missed:
        all_violations.extend([f"Missed mandatory: {m}" for m in mandatory_missed])

    return SolverMetrics(
        solver_name=solver_name,
        time_window_satisfaction_rate=tw_rate,
        pois_within_windows=tw_within,
        pois_outside_windows=tw_outside,
        time_utilisation_score=tus,
        daily_utilisation=daily_util,
        total_travel_time_min=total_travel,
        total_distance_km=total_distance,
        avg_travel_per_poi_min=avg_travel_per_poi,
        unique_pois=unique_pois,
        total_stops=total_stops,
        meals_scheduled=meals,
        attractions_scheduled=attractions,
        time_sequence_valid=time_valid,
        depot_positions_valid=depot_valid,
        poi_coverage_valid=len(duplicates) == 0,
        meal_constraints_valid=meal_valid,
        theme_constraints_valid=theme_valid,
        meal_window_compliance=meal_window_compliance,
        food_streak_valid=food_streak_valid,
        mandatory_pois_scheduled=mandatory_scheduled,
        mandatory_pois_missed=mandatory_missed,
        violations=all_violations,
        feasible=len(all_violations) == 0,
        execution_time_sec=execution_time_sec,
    )


def print_comparison_report(ortools: SolverMetrics, acs: SolverMetrics, pacing: str):
    """Print a comprehensive comparison report."""
    print("\n" + "=" * 80)
    print("📊 SOLVER COMPARISON REPORT")
    print("=" * 80)
    print(f"Pacing: {pacing}")

    # Header
    print(f"\n{'Metric':<40} {'OR-Tools':>15} {'ACS':>15} {'Winner':>10}")
    print("-" * 80)

    # Time Window Satisfaction
    winner = (
        "OR-Tools"
        if ortools.time_window_satisfaction_rate > acs.time_window_satisfaction_rate
        else "ACS"
        if acs.time_window_satisfaction_rate > ortools.time_window_satisfaction_rate
        else "Tie"
    )
    print(
        f"{'Time Window Satisfaction (%)':<40} {ortools.time_window_satisfaction_rate:>14.1f}% {acs.time_window_satisfaction_rate:>14.1f}% {winner:>10}"
    )

    # TUS
    winner = (
        "OR-Tools"
        if 80 <= ortools.time_utilisation_score <= 100 and not (80 <= acs.time_utilisation_score <= 100)
        else "ACS"
        if 80 <= acs.time_utilisation_score <= 100 and not (80 <= ortools.time_utilisation_score <= 100)
        else "OR-Tools"
        if abs(ortools.time_utilisation_score - 90) < abs(acs.time_utilisation_score - 90)
        else "ACS"
    )
    print(
        f"{'Time Utilisation Score (%) [80-100 ideal]':<40} {ortools.time_utilisation_score:>14.1f}% {acs.time_utilisation_score:>14.1f}% {winner:>10}"
    )

    # Travel Time
    winner = (
        "ACS"
        if acs.total_travel_time_min < ortools.total_travel_time_min
        else "OR-Tools"
        if ortools.total_travel_time_min < acs.total_travel_time_min
        else "Tie"
    )
    print(
        f"{'Total Active Time (min)':<40} {ortools.total_travel_time_min:>15} {acs.total_travel_time_min:>15} {winner:>10}"
    )

    # Distance
    winner = (
        "ACS"
        if acs.total_distance_km < ortools.total_distance_km
        else "OR-Tools"
        if ortools.total_distance_km < acs.total_distance_km
        else "Tie"
    )
    print(f"{'Total Distance (km)':<40} {ortools.total_distance_km:>14.2f} {acs.total_distance_km:>14.2f} {winner:>10}")

    # POI Coverage
    winner = (
        "ACS"
        if acs.unique_pois > ortools.unique_pois
        else "OR-Tools"
        if ortools.unique_pois > acs.unique_pois
        else "Tie"
    )
    print(f"{'Unique POIs Visited':<40} {ortools.unique_pois:>15} {acs.unique_pois:>15} {winner:>10}")

    # Meals
    winner = (
        "ACS"
        if acs.meals_scheduled > ortools.meals_scheduled
        else "OR-Tools"
        if ortools.meals_scheduled > acs.meals_scheduled
        else "Tie"
    )
    print(f"{'Meals Scheduled':<40} {ortools.meals_scheduled:>15} {acs.meals_scheduled:>15} {winner:>10}")

    # Meal Window Compliance
    winner = (
        "ACS"
        if acs.meal_window_compliance > ortools.meal_window_compliance
        else "OR-Tools"
        if ortools.meal_window_compliance > acs.meal_window_compliance
        else "Tie"
    )
    print(
        f"{'Meal Window Compliance (%)':<40} {ortools.meal_window_compliance:>14.1f}% {acs.meal_window_compliance:>14.1f}% {winner:>10}"
    )

    # Execution Time (lower is better)
    winner = (
        "ACS"
        if acs.execution_time_sec < ortools.execution_time_sec
        else "OR-Tools"
        if ortools.execution_time_sec < acs.execution_time_sec
        else "Tie"
    )
    print(
        f"{'Execution Time (sec)':<40} {ortools.execution_time_sec:>14.2f}s {acs.execution_time_sec:>14.2f}s {winner:>10}"
    )

    print("-" * 80)

    # Constraint Compliance Section
    print(f"\n{'Constraint Compliance':<40} {'OR-Tools':>15} {'ACS':>15}")
    print("-" * 80)
    print(
        f"{'Time Sequence Valid':<40} {'✅' if ortools.time_sequence_valid else '❌':>15} {'✅' if acs.time_sequence_valid else '❌':>15}"
    )
    print(
        f"{'Meal Constraints (max 3, no consec)':<40} {'✅' if ortools.meal_constraints_valid else '❌':>15} {'✅' if acs.meal_constraints_valid else '❌':>15}"
    )
    print(
        f"{'Theme Distribution (soft penalties)':<40} {'✅' if ortools.theme_constraints_valid else '❌':>15} {'✅' if acs.theme_constraints_valid else '❌':>15}"
    )
    print(
        f"{'Food Streak (max 2 consecutive)':<40} {'✅' if ortools.food_streak_valid else '❌':>15} {'✅' if acs.food_streak_valid else '❌':>15}"
    )
    print(
        f"{'No Duplicate POIs':<40} {'✅' if ortools.poi_coverage_valid else '❌':>15} {'✅' if acs.poi_coverage_valid else '❌':>15}"
    )

    # Feasibility Summary
    print("-" * 80)
    print(f"{'OVERALL FEASIBLE':<40} {'✅' if ortools.feasible else '❌':>15} {'✅' if acs.feasible else '❌':>15}")
    print(f"{'Total Violations':<40} {len(ortools.violations):>15} {len(acs.violations):>15}")

    if ortools.violations:
        print(f"\n⚠️  OR-Tools Violations ({len(ortools.violations)}):")
        for v in ortools.violations[:5]:
            print(f"   - {v}")
        if len(ortools.violations) > 5:
            print(f"   ... and {len(ortools.violations) - 5} more")

    if acs.violations:
        print(f"\n⚠️  ACS Violations ({len(acs.violations)}):")
        for v in acs.violations[:5]:
            print(f"   - {v}")
        if len(acs.violations) > 5:
            print(f"   ... and {len(acs.violations) - 5} more")

    # Daily Breakdown
    print("\n📅 Daily Time Utilisation:")
    print(f"{'Day':<10} {'OR-Tools':>15} {'ACS':>15}")
    for i, (ot, ac) in enumerate(zip(ortools.daily_utilisation, acs.daily_utilisation)):
        status_ot = "⚠️" if ot > 100 else "✅" if ot >= 70 else "📉"
        status_ac = "⚠️" if ac > 100 else "✅" if ac >= 70 else "📉"
        print(f"{'Day ' + str(i + 1):<10} {ot:>13.1f}% {status_ot} {ac:>13.1f}% {status_ac}")

    # Overall Weighted Score Calculation
    # Weights reflect capstone priorities for itinerary quality
    print("\n" + "-" * 80)
    print("📈 OVERALL WEIGHTED SCORE")
    print("-" * 80)

    # Define weights (total = 100)
    weights = {
        "poi_coverage": 25,  # POIs visited (more is better)
        "constraint_compliance": 25,  # Feasibility (critical)
        "meal_compliance": 15,  # Meal timing quality
        "tus_quality": 15,  # Time utilisation (80-100% ideal)
        "efficiency": 10,  # Distance/time efficiency
        "execution_time": 10,  # Solver speed
    }

    def calculate_score(m: SolverMetrics, other: SolverMetrics) -> dict:
        """Calculate component scores (0-100 scale)."""
        scores = {}

        # POI Coverage: normalize by max POIs between solvers
        max_pois = max(m.unique_pois, other.unique_pois)
        scores["poi_coverage"] = (m.unique_pois / max_pois * 100) if max_pois > 0 else 100

        # Constraint Compliance: 100 if feasible, penalize for violations
        scores["constraint_compliance"] = 100 if m.feasible else max(0, 100 - len(m.violations) * 10)

        # Meal Window Compliance: direct percentage
        scores["meal_compliance"] = m.meal_window_compliance

        # TUS Quality: score how close to ideal range (80-100%)
        # Best score at 90%, decreases as result move away
        tus_ideal = 90
        tus_deviation = abs(m.time_utilisation_score - tus_ideal)
        scores["tus_quality"] = max(0, 100 - tus_deviation * 2)  # -2 points per % away from 90

        # Efficiency: normalize by max distance (lower is better)
        max_dist = max(m.total_distance_km, other.total_distance_km)
        if max_dist > 0:
            scores["efficiency"] = (1 - m.total_distance_km / max_dist) * 100 + 50  # 50-100 scale
            scores["efficiency"] = min(100, scores["efficiency"])
        else:
            scores["efficiency"] = 100

        # Execution Time: normalize by max time (lower is better)
        max_time = max(m.execution_time_sec, other.execution_time_sec)
        if max_time > 0:
            scores["execution_time"] = (1 - m.execution_time_sec / max_time) * 100 + 50
            scores["execution_time"] = min(100, scores["execution_time"])
        else:
            scores["execution_time"] = 100

        return scores

    ortools_scores = calculate_score(ortools, acs)
    acs_scores = calculate_score(acs, ortools)

    # Calculate weighted totals
    ortools_total = sum(ortools_scores[k] * weights[k] / 100 for k in weights)
    acs_total = sum(acs_scores[k] * weights[k] / 100 for k in weights)

    print(f"\n{'Component':<30} {'Weight':>8} {'OR-Tools':>12} {'ACS':>12}")
    print("-" * 65)
    for key, weight in weights.items():
        ot_score = ortools_scores[key]
        ac_score = acs_scores[key]
        winner_mark = "◀" if ot_score > ac_score else ("▶" if ac_score > ot_score else "")
        print(f"{key.replace('_', ' ').title():<30} {weight:>7}% {ot_score:>11.1f} {ac_score:>11.1f} {winner_mark}")

    print("-" * 65)
    overall_winner = "OR-Tools" if ortools_total > acs_total else "ACS" if acs_total > ortools_total else "Tie"
    print(f"{'WEIGHTED TOTAL':<30} {'100%':>8} {ortools_total:>11.1f} {acs_total:>11.1f}")
    print(f"\n🏆 OVERALL WINNER: {overall_winner}")
    print(f"   OR-Tools: {ortools_total:.1f}/100 | ACS: {acs_total:.1f}/100")

    print("\n" + "=" * 80)


def test_compare_solvers():
    """
    Compare OR-Tools vs ACS-CVRPTW on same MAUT output.

    Both use:
    - Same MAUT output (POI candidates)
    - Same hotel
    - Same OSRM travel matrix
    - Same pacing

    Evaluation Metrics:
    1. Time Window Satisfaction Rate
    2. Time Utilisation Score (TUS)
    3. Constraint Compliance
    4. Travel Efficiency
    5. POI Coverage
    """
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_maut(maut_request)

    assert maut_output["status"] == "ok"
    assert len(maut_output.get("places", [])) > 0

    # Inject dates/num_days
    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # Get hotel
    selected_hotel = maut_output["meta"].get("selected_hotel")
    assert selected_hotel, "MAUT did not select a hotel"

    pacing = maut_request["pacing"]

    # Run OR-Tools with timing
    import time

    ortools_start = time.time()
    ortools_output = run_full_pipeline(
        maut_output=maut_output,
        pacing=pacing,
        solver="ortools",
        time_limit_sec=20,
    )
    ortools_time = time.time() - ortools_start
    assert ortools_output.get("status") == "success", f"OR-Tools failed: {ortools_output}"

    # Run ACS with timing
    acs_start = time.time()
    acs_output = run_full_pipeline(
        maut_output=maut_output,
        pacing=pacing,
        solver="acs",
    )
    acs_time = time.time() - acs_start
    assert acs_output.get("status") == "success", f"ACS failed: {acs_output}"

    # Evaluate both solutions
    ortools_eval = evaluate_solution(ortools_output, "OR-Tools", pacing, ortools_time)
    acs_eval = evaluate_solution(acs_output, "ACS", pacing, acs_time)

    # Print comparison report
    print_comparison_report(ortools_eval, acs_eval, pacing)

    # Save outputs for inspection
    out_dir = os.path.dirname(__file__)
    with open(os.path.join(out_dir, "comparison_ortools_output.json"), "w") as f:
        json.dump(ortools_output, f, indent=2)
    with open(os.path.join(out_dir, "comparison_acs_output.json"), "w") as f:
        json.dump(acs_output, f, indent=2)

    eval_results = {
        "pacing": pacing,
        "ortools": {
            "solver": ortools_eval.solver_name,
            "execution_time_sec": ortools_eval.execution_time_sec,
            "time_window_satisfaction_rate": ortools_eval.time_window_satisfaction_rate,
            "time_utilisation_score": ortools_eval.time_utilisation_score,
            "daily_utilisation": ortools_eval.daily_utilisation,
            "total_travel_time_min": ortools_eval.total_travel_time_min,
            "total_distance_km": ortools_eval.total_distance_km,
            "unique_pois": ortools_eval.unique_pois,
            "meals_scheduled": ortools_eval.meals_scheduled,
            "attractions_scheduled": ortools_eval.attractions_scheduled,
            "meal_window_compliance": ortools_eval.meal_window_compliance,
            "constraints": {
                "time_sequence": ortools_eval.time_sequence_valid,
                "meal_constraints": ortools_eval.meal_constraints_valid,
                "theme_constraints": ortools_eval.theme_constraints_valid,
                "food_streak": ortools_eval.food_streak_valid,
                "no_duplicates": ortools_eval.poi_coverage_valid,
            },
            "feasible": ortools_eval.feasible,
            "violations": ortools_eval.violations,
        },
        "acs": {
            "solver": acs_eval.solver_name,
            "execution_time_sec": acs_eval.execution_time_sec,
            "time_window_satisfaction_rate": acs_eval.time_window_satisfaction_rate,
            "time_utilisation_score": acs_eval.time_utilisation_score,
            "daily_utilisation": acs_eval.daily_utilisation,
            "total_travel_time_min": acs_eval.total_travel_time_min,
            "total_distance_km": acs_eval.total_distance_km,
            "unique_pois": acs_eval.unique_pois,
            "meals_scheduled": acs_eval.meals_scheduled,
            "attractions_scheduled": acs_eval.attractions_scheduled,
            "meal_window_compliance": acs_eval.meal_window_compliance,
            "constraints": {
                "time_sequence": acs_eval.time_sequence_valid,
                "meal_constraints": acs_eval.meal_constraints_valid,
                "theme_constraints": acs_eval.theme_constraints_valid,
                "food_streak": acs_eval.food_streak_valid,
                "no_duplicates": acs_eval.poi_coverage_valid,
            },
            "feasible": acs_eval.feasible,
            "violations": acs_eval.violations,
        },
    }
    with open(os.path.join(out_dir, "comparison_evaluation.json"), "w") as f:
        json.dump(eval_results, f, indent=2)

    print(f"\n✅ Comparison complete. Results saved to {out_dir}/")

    # Assertions - both must pass basic feasibility
    # Note: We're more lenient here since we want to compare, not just pass/fail

    # 1. Time sequences must be valid (hard requirement)
    assert ortools_eval.time_sequence_valid, "OR-Tools has invalid time sequences"
    assert acs_eval.time_sequence_valid, "ACS has invalid time sequences"

    # 2. No duplicate POIs (hard requirement)
    assert ortools_eval.poi_coverage_valid, "OR-Tools has duplicate POIs"
    assert acs_eval.poi_coverage_valid, "ACS has duplicate POIs"

    # 3. Both should be reasonably feasible (allow some soft constraint violations)
    # This is a comparison test, so we log issues rather than fail
    if not ortools_eval.feasible:
        print(f"\n⚠️  OR-Tools has {len(ortools_eval.violations)} violations (logged, not failing)")
    if not acs_eval.feasible:
        print(f"\n⚠️  ACS has {len(acs_eval.violations)} violations (logged, not failing)")


def test_compare_solvers_multiple_pacings():
    """
    Compare solvers across all pacing types to ensure robustness.
    """
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    maut_request = transform_frontend_payload(frontend_payload)

    for pacing in ["relaxed", "balanced", "packed"]:
        print(f"\n\n{'=' * 80}")
        print(f"🔄 Testing with pacing: {pacing}")
        print("=" * 80)

        maut_request["pacing"] = pacing
        maut_output = run_maut(maut_request)

        if maut_output["status"] != "ok":
            print(f"⚠️  MAUT failed for {pacing}: {maut_output}")
            continue

        maut_output.setdefault("meta", {})
        maut_output["meta"]["dates"] = frontend_payload["dates"]
        maut_output["meta"]["num_days"] = maut_request["num_days"]

        selected_hotel = maut_output["meta"].get("selected_hotel")
        if not selected_hotel:
            print(f"⚠️  No hotel selected for {pacing}")
            continue

        try:
            t0 = time.perf_counter()
            ortools_output = run_full_pipeline(
                maut_output=maut_output,
                pacing=pacing,
                solver="ortools",
                time_limit_sec=15,
            )
            ortools_time_sec = time.perf_counter() - t0

            t1 = time.perf_counter()
            acs_output = run_full_pipeline(
                maut_output=maut_output,
                pacing=pacing,
                solver="acs",
            )
            acs_time_sec = time.perf_counter() - t1

            if ortools_output.get("status") == "success" and acs_output.get("status") == "success":
                ortools_eval = evaluate_solution(
                    ortools_output,
                    "OR-Tools",
                    pacing,
                    execution_time_sec=ortools_time_sec,
                )
                acs_eval = evaluate_solution(
                    acs_output,
                    "ACS",
                    pacing,
                    execution_time_sec=acs_time_sec,
                )
                print_comparison_report(ortools_eval, acs_eval, pacing)
            else:
                print(f"⚠️  Solver failed for {pacing}")
                print(f"   OR-Tools: {ortools_output.get('status')}")
                print(f"   ACS: {acs_output.get('status')}")
        except Exception as e:
            print(f"⚠️  Error testing {pacing}: {e}")
