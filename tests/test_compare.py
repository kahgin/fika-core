"""
Comparison test: ACS-CVRPTW vs OR-Tools CVRPTW.

Purpose: Compare solution quality between the two solvers on the same input.
Metrics: Travel time, POI coverage, feasibility, constraints compliance.
"""

import os
import json
from typing import Dict, List, Any, Set

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_spec.json")


def _time_to_minutes(time_str: str) -> int:
    """Convert HH:MM to minutes since midnight."""
    try:
        h, m = time_str.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _validate_time_sequence(stops: List[Dict]) -> Dict[str, Any]:
    """
    Validate that arrival/departure times are feasible in sequence.

    Returns dict with:
    - valid: bool
    - violations: list of issues
    """
    violations = []

    for i in range(len(stops) - 1):
        curr = stops[i]
        next_stop = stops[i + 1]

        curr_depart = _time_to_minutes(curr.get("depart", "00:00"))
        next_arrival = _time_to_minutes(next_stop.get("arrival", "00:00"))

        if next_arrival < curr_depart:
            violations.append(
                f"Stop {i}->{i + 1}: Next arrival ({next_stop.get('arrival')}) "
                f"before current departure ({curr.get('depart')})"
            )

    return {"valid": len(violations) == 0, "violations": violations}


def _check_depot_positions(stops: List[Dict]) -> Dict[str, Any]:
    """Check if depot is first and last stop."""
    issues = []

    if not stops:
        return {"valid": False, "issues": ["No stops"]}

    first = stops[0]
    last = stops[-1]

    if first.get("role") not in ("depot", "hotel"):
        issues.append(f"First stop is not depot: {first.get('role')}")

    if last.get("role") not in ("depot", "hotel"):
        issues.append(f"Last stop is not depot: {last.get('role')}")

    return {"valid": len(issues) == 0, "issues": issues}


def _get_base_poi_id(poi_id: str) -> str:
    """Strip _dayX suffix to get base POI ID."""
    return poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id


def _check_poi_coverage(days: List[Dict]) -> Dict[str, Any]:
    """
    Check POI coverage:
    - Each base POI visited at most once
    - Count total unique POIs
    """
    visited_base_ids: Set[str] = set()
    duplicates = []
    total_stops = 0

    for day_idx, day in enumerate(days):
        for stop in day.get("stops", []):
            if stop.get("role") in ("depot", "hotel"):
                continue

            total_stops += 1
            base_id = _get_base_poi_id(stop.get("poi_id", ""))

            if base_id in visited_base_ids:
                duplicates.append(f"Day {day_idx + 1}: {stop.get('name')} ({base_id})")
            else:
                visited_base_ids.add(base_id)

    return {
        "unique_pois": len(visited_base_ids),
        "total_stops": total_stops,
        "duplicates": duplicates,
        "valid": len(duplicates) == 0,
    }


def _check_meal_constraints(days: List[Dict]) -> Dict[str, Any]:
    """
    Check meal constraints:
    - Max 3 meals per day
    - No consecutive meals
    """
    violations = []

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        meal_count = sum(1 for s in stops if s.get("role") == "meal")

        if meal_count > 3:
            violations.append(f"Day {day_idx + 1}: {meal_count} meals (max 3)")

        # Check consecutive meals
        for i in range(len(stops) - 1):
            if stops[i].get("role") == "meal" and stops[i + 1].get("role") == "meal":
                violations.append(
                    f"Day {day_idx + 1}: Consecutive meals at stops {i}-{i + 1}"
                )

    return {"valid": len(violations) == 0, "violations": violations}


def _check_theme_constraints(
    days: List[Dict], max_per_theme: int = 2
) -> Dict[str, Any]:
    """
    Check theme diversity:
    - Max 2 attractions with same primary theme per day
    """
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

        for theme, count in theme_counts.items():
            if count > max_per_theme:
                violations.append(
                    f"Day {day_idx + 1}: {count} attractions with theme '{theme}' (max {max_per_theme})"
                )

    return {"valid": len(violations) == 0, "violations": violations}


def _calculate_total_travel_time(days: List[Dict]) -> int:
    """
    Calculate total travel time including service times.
    Returns total minutes.
    """
    total_minutes = 0

    for day in days:
        stops = day.get("stops", [])
        if not stops:
            continue

        first_arrival = _time_to_minutes(stops[0].get("arrival", "00:00"))
        last_depart = _time_to_minutes(stops[-1].get("depart", "00:00"))

        day_duration = last_depart - first_arrival
        total_minutes += day_duration

    return total_minutes


def _evaluate_solution(output: Dict[str, Any], solver_name: str) -> Dict[str, Any]:
    """
    Comprehensive evaluation of a solver's solution.

    Returns metrics dict with:
    - total_travel_time_min
    - unique_pois
    - time_sequence_valid
    - depot_positions_valid
    - poi_coverage_valid
    - meal_constraints_valid
    - theme_constraints_valid
    - total_distance
    - violations (list of all issues)
    """
    days = output.get("days", [])
    meta = output.get("meta", {})

    all_violations = []

    # 1. Total travel time
    total_travel_time = _calculate_total_travel_time(days)

    # 2. POI coverage
    poi_coverage = _check_poi_coverage(days)
    if not poi_coverage["valid"]:
        all_violations.extend(poi_coverage["duplicates"])

    # 3. Time sequence validation (per day)
    time_valid = True
    for day_idx, day in enumerate(days):
        seq_check = _validate_time_sequence(day.get("stops", []))
        if not seq_check["valid"]:
            time_valid = False
            all_violations.extend(
                [f"Day {day_idx + 1}: {v}" for v in seq_check["violations"]]
            )

    # 4. Depot positions (per day)
    depot_valid = True
    for day_idx, day in enumerate(days):
        depot_check = _check_depot_positions(day.get("stops", []))
        if not depot_check["valid"]:
            depot_valid = False
            all_violations.extend(
                [f"Day {day_idx + 1}: {i}" for i in depot_check["issues"]]
            )

    # 5. Meal constraints
    meal_check = _check_meal_constraints(days)
    if not meal_check["valid"]:
        all_violations.extend(meal_check["violations"])

    # 6. Theme constraints
    theme_check = _check_theme_constraints(days)
    if not theme_check["valid"]:
        all_violations.extend(theme_check["violations"])

    return {
        "solver": solver_name,
        "total_travel_time_min": total_travel_time,
        "unique_pois": poi_coverage["unique_pois"],
        "total_stops": poi_coverage["total_stops"],
        "time_sequence_valid": time_valid,
        "depot_positions_valid": depot_valid,
        "poi_coverage_valid": poi_coverage["valid"],
        "meal_constraints_valid": meal_check["valid"],
        "theme_constraints_valid": theme_check["valid"],
        "total_distance_km": meta.get("total_distance", 0),
        "violations": all_violations,
        "feasible": len(all_violations) == 0,
    }


def test_compare_solvers():
    """
    Compare OR-Tools vs ACS-CVRPTW on same MAUT output.

    Both use:
    - Same MAUT output (POI candidates)
    - Same hotel
    - Same OSRM travel matrix
    - Same pacing

    Comparison Metrics (Priority Order):
    1. Total Travel Time (most important)
    2. POI Coverage (more POIs is better when constraints respected)
    3. Feasibility (all constraints must be valid)
    4. Total Distance (optional, for reference)
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

    coords = selected_hotel.get("coordinates") or {}
    hotel = {
        "id": selected_hotel["id"],
        "name": selected_hotel["name"],
        "lat": coords.get("lat"),
        "lon": coords.get("lng"),
    }

    # Run OR-Tools
    ortools_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        solver="ortools",
        time_limit_sec=20,
    )
    assert ortools_output.get("status") == "success"

    # Run ACS
    acs_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        solver="acs",
    )
    assert acs_output.get("status") == "success"

    # Evaluate both solutions
    ortools_eval = _evaluate_solution(ortools_output, "OR-Tools")
    acs_eval = _evaluate_solution(acs_output, "ACS")

    # Print comparison
    print("\n" + "=" * 80)
    print("📊 SOLVER COMPARISON REPORT")
    print("=" * 80)

    print("\n🔧 OR-Tools Results:")
    print(f"   Total Travel Time: {ortools_eval['total_travel_time_min']} min")
    print(f"   Unique POIs: {ortools_eval['unique_pois']}")
    print(f"   Total Stops: {ortools_eval['total_stops']}")
    print(f"   Total Distance: {ortools_eval['total_distance_km']:.2f} km")
    print(f"   Feasible: {ortools_eval['feasible']}")
    if ortools_eval["violations"]:
        print(f"   ⚠️  Violations: {len(ortools_eval['violations'])}")
        for v in ortools_eval["violations"][:5]:
            print(f"      - {v}")

    print("\n🐜 ACS Results:")
    print(f"   Total Travel Time: {acs_eval['total_travel_time_min']} min")
    print(f"   Unique POIs: {acs_eval['unique_pois']}")
    print(f"   Total Stops: {acs_eval['total_stops']}")
    print(f"   Total Distance: {acs_eval['total_distance_km']:.2f} km")
    print(f"   Feasible: {acs_eval['feasible']}")
    if acs_eval["violations"]:
        print(f"   ⚠️  Violations: {len(acs_eval['violations'])}")
        for v in acs_eval["violations"][:5]:
            print(f"      - {v}")

    print("\n📈 Comparison:")

    # Travel time comparison (most important)
    if ortools_eval["total_travel_time_min"] > 0:
        time_ratio = (
            acs_eval["total_travel_time_min"] / ortools_eval["total_travel_time_min"]
        )
        print(f"   Travel Time Ratio (ACS/OR-Tools): {time_ratio:.2f}x")
        if time_ratio < 1.0:
            print(f"   ✅ ACS is {(1 - time_ratio) * 100:.1f}% faster")
        else:
            print(f"   ⚠️  ACS is {(time_ratio - 1) * 100:.1f}% slower")

    # POI coverage comparison
    poi_diff = acs_eval["unique_pois"] - ortools_eval["unique_pois"]
    if poi_diff > 0:
        print(f"   ✅ ACS visits {poi_diff} more POIs")
    elif poi_diff < 0:
        print(f"   ⚠️  ACS visits {abs(poi_diff)} fewer POIs")
    else:
        print(f"   ✓ Same POI coverage")

    # Distance comparison (optional)
    if ortools_eval["total_distance_km"] > 0:
        dist_ratio = acs_eval["total_distance_km"] / ortools_eval["total_distance_km"]
        print(f"   Distance Ratio (ACS/OR-Tools): {dist_ratio:.2f}x")

    print("\n" + "=" * 80)

    # Assertions
    # 1. Both must be feasible
    assert ortools_eval["feasible"], (
        f"OR-Tools solution has violations: {ortools_eval['violations']}"
    )
    assert acs_eval["feasible"], (
        f"ACS solution has violations: {acs_eval['violations']}"
    )

    # 2. Both must have valid time sequences
    assert ortools_eval["time_sequence_valid"], "OR-Tools has invalid time sequences"
    assert acs_eval["time_sequence_valid"], "ACS has invalid time sequences"

    # 3. Both must have valid depot positions
    assert ortools_eval["depot_positions_valid"], "OR-Tools has invalid depot positions"
    assert acs_eval["depot_positions_valid"], "ACS has invalid depot positions"

    # 4. Both must respect meal constraints
    assert ortools_eval["meal_constraints_valid"], "OR-Tools violates meal constraints"
    assert acs_eval["meal_constraints_valid"], "ACS violates meal constraints"

    # 5. Both must respect theme constraints
    assert ortools_eval["theme_constraints_valid"], (
        "OR-Tools violates theme constraints"
    )
    assert acs_eval["theme_constraints_valid"], "ACS violates theme constraints"

    # 6. ACS should not be catastrophically worse on travel time
    # Allow up to 50% worse travel time (since it's a heuristic)
    if ortools_eval["total_travel_time_min"] > 0:
        time_ratio = (
            acs_eval["total_travel_time_min"] / ortools_eval["total_travel_time_min"]
        )
        assert time_ratio <= 1.5, (
            f"ACS travel time is {time_ratio:.2f}x worse than OR-Tools (max 1.5x allowed)"
        )

    # 7. ACS should visit at least as many POIs (when feasible)
    # This is a soft check - we prefer more POIs if constraints are met
    if acs_eval["unique_pois"] < ortools_eval["unique_pois"]:
        print(
            f"\n⚠️  Note: ACS visits fewer POIs ({acs_eval['unique_pois']} vs {ortools_eval['unique_pois']})"
        )

    # Save outputs for inspection
    out_dir = os.path.dirname(__file__)
    with open(os.path.join(out_dir, "comparison_ortools_output.json"), "w") as f:
        json.dump(ortools_output, f, indent=2)
    with open(os.path.join(out_dir, "comparison_acs_output.json"), "w") as f:
        json.dump(acs_output, f, indent=2)

    # Save evaluation results
    with open(os.path.join(out_dir, "comparison_evaluation.json"), "w") as f:
        json.dump({"ortools": ortools_eval, "acs": acs_eval}, f, indent=2)

    print(f"\n✅ Comparison complete. Results saved to {out_dir}/")
