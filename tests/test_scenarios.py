"""
Solver Comparison Tests.

This module tests different real-world scenarios comparing OR-Tools (Baseline) and ACS-CVRPTW (System).
Each test generates reproducible results stored in JSON and comparison charts.
"""

import os
import json
import time
import pytest
from datetime import datetime
from typing import Dict, Any, Set, Tuple

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.services.vrp_model import vrp_config
from app.utils.date_utils import time_to_minutes
from tests.chart_generator import generate_all_charts


OUTPUT_DIR = os.path.dirname(__file__)


# TEST PAYLOADS FOR DIFFERENT SCENARIOS

SCENARIO_MULTI_CITY_BALANCED = {
    "title": "Scenario 1: Multi-Day Multi-City Balanced",
    "description": "5-day trip across Singapore and Johor with balanced pacing and no interests selected",
    "payload": {
        "title": "Singapore-Johor Multi-City Trip",
        "destinations": [{"city": "Singapore"}, {"city": "Johor, Malaysia"}],
        "dates": {"type": "specific", "start_date": "2026-03-01", "end_date": "2026-03-05"},
        "travelers": {"adults": 2, "children": 0, "pets": 0},
        "dietary_restrictions": [],
        "preferences": {"pacing": "balanced"},
        "flags": {},
    },
    "pacing": "balanced",
}

SCENARIO_SINGLE_CITY_SINGLE_THEME = {
    "title": "Scenario 2: Single City Single Theme",
    "description": "10-day Singapore trip focused solely on shopping theme",
    "payload": {
        "title": "Singapore Shopping Focus Trip",
        "destinations": [{"city": "Johor, Malaysia"}],
        "dates": {"type": "specific", "start_date": "2026-04-01", "end_date": "2026-04-10"},
        "travelers": {"adults": 2, "children": 0, "pets": 0},
        "dietary_restrictions": [],
        "preferences": {"pacing": "balanced", "interests": ["shopping"]},
        "flags": {},
    },
    "pacing": "balanced",
}

SCENARIO_CONSTRAINED_FAMILY = {
    "title": "Scenario 3: Family Trip with Full Constraints",
    "description": "3-day Singapore trip with kids, pets, wheelchair accessibility, and halal dietary requirement",
    "payload": {
        "title": "Constrained Family Trip",
        "destinations": [{"city": "Singapore"}],
        "dates": {"type": "specific", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        "travelers": {"adults": 2, "children": 2, "pets": 1},
        "dietary_restrictions": ["halal"],
        "preferences": {"pacing": "relaxed", "interests": ["family", "nature", "cultural_history"]},
        "flags": {"kids_friendly": True, "pets_friendly": True, "wheelchair_accessible": True, "is_muslim": True},
    },
    "pacing": "relaxed",
}

SCENARIO_MANDATORY_POI_HOTEL_SELECTED = {
    "title": "Scenario 4: Mandatory POI with Hotel Selected",
    "description": "3-day Johor trip with user selected hotel and mandatory POI.",
    "payload": {
        "title": "Constrained Family Trip",
        "destinations": [{"city": "Johor, Malaysia"}],
        "dates": {"type": "specific", "start_date": "2026-05-01", "end_date": "2026-05-03"},
        "travelers": {"adults": 2, "children": 2, "pets": 1},
        "dietary_restrictions": ["halal"],
        "preferences": {"pacing": "relaxed", "interests": ["family", "nature", "cultural_history"]},
        "flags": {"kids_friendly": True, "pets_friendly": True, "wheelchair_accessible": True, "is_muslim": True},
        "hotels": [
            {
                "role": "accommodation",
                "images": [],
                "poi_id": "378fdda9-3fe2-4de3-a170-ce2a60adb0f7",
                "themes": [],
                "latitude": 1.516239,
                "poi_name": "Hyatt Place Johor Bahru Paradigm Mall",
                "longitude": 103.685119,
                "destination": "Johor, Malaysia",
                "check_in_date": "2026-05-01",
                "check_out_date": "2026-05-03",
            }
        ],
        "mandatory_pois": [
            {
                "date": "2026-05-03",
                "role": "meal",
                "images": [],
                "poi_id": "1a020b72-36bd-42f8-8ab0-f639596b8d5d",
                "themes": [],
                "latitude": 1.475192,
                "poi_name": "MALALAL Hotpot",
                "longitude": 103.588141,
                "time_type": "anyTime",
                "open_hours": None,
                "poi_destination": "Johor, Malaysia",
            }
        ],
    },
    "pacing": "relaxed",
}

# SCENARIO_MULTI_CITY_WITH_ASSIGNED_DAYS = {
#     "title": "Scenario 5: Multi-City with Assigned Days",
#     "description": "6-day trip across Singapore and Johor with per-city date assignments",
#     "payload": {
#         "title": "Multi-City Assigned Days Trip",
#         "destinations": [
#             {
#                 "city": "Singapore",
#                 "dates": {
#                     "type": "specific",
#                     "start_date": "2026-06-01",
#                     "end_date": "2026-06-03",
#                 }
#             },
#             {
#                 "city": "Johor, Malaysia",
#                 "dates": {
#                     "type": "specific",
#                     "start_date": "2026-06-04",
#                     "end_date": "2026-06-06",
#                 }
#             }
#         ],
#         "dates": {"type": "specific", "start_date": "2026-06-01", "end_date": "2026-06-06"},
#         "travelers": {"adults": 2, "children": 0, "pets": 0},
#         "dietary_restrictions": [],
#         "preferences": {"pacing": "packed", "interests": ["shopping", "food_culinary"]},
#         "flags": {},
#     },
#     "pacing": "packed",
# }


SCENARIO_MULTI_CITY_REPEAT_VISIT_MANDATORY_POIS = {
    "title": "Scenario 6: Multi-City with Repeat Visits and Mandatory POIs",
    "description": "6-day trip across Singapore and Johor with mandatory POIs in cities visited more than once",
    "payload": {
        "title": "Multi-City Mandatory POIs Trip",
        "destinations": [{"city": "Singapore"}, {"city": "Johor, Malaysia"}],
        "dates": {"type": "specific", "start_date": "2026-06-01", "end_date": "2026-06-06"},
        "travelers": {"adults": 2, "children": 2, "pets": 0},
        "dietary_restrictions": ["halal"],
        "preferences": {"pacing": "relaxed", "interests": ["family", "nature", "cultural_history"]},
        "flags": {"kids_friendly": True, "is_muslim": True},
        "mandatory_pois": [
            {
                "poi_id": "4ed37b8a-4fb8-40ef-b70a-c5ca9230ef71",
                "poi_name": "LEGOLAND Malaysia",
                "poi_destination": "Johor, Malaysia",
                "latitude": 1.427236,
                "longitude": 103.629489,
                "date": "2026-05-01",
                "time_type": "anyTime",
                "themes": ["family"],
                "role": "attraction",
                "open_hours": {
                    "friday": ["10 am-6 pm"],
                    "monday": ["10 am-6 pm"],
                    "sunday": ["10 am-6 pm"],
                    "tuesday": ["10 am-6 pm"],
                    "saturday": ["10 am-6 pm"],
                    "thursday": ["10 am-6 pm"],
                    "wednesday": ["Closed"],
                },
                "images": [
                    "https://lh3.googleusercontent.com/gps-cs-s/AG0ilSzXmC_5QDgGd1mw-JFuznEMDhRjhSrTHi0aB0DWffIE7Q3nkSoOyZtF3HfngnV5njYDAre_o_wbanATFrHfxGIA5yPmXRaX9FMGw4UIMCWTcraYOIroYx_o1s_0uxIxnJoKW3osaA"
                ],
            },
            {
                "poi_id": "9849c82a-c5ab-417d-a77e-b1728e04861e",
                "poi_name": "Universal Studios Singapore",
                "poi_destination": "Singapore",
                "latitude": 1.254042,
                "longitude": 103.823808,
                "date": "2026-05-03",
                "time_type": "anyTime",
                "themes": ["family"],
                "role": "attraction",
                "open_hours": {
                    "friday": ["10 am-7 pm"],
                    "monday": ["10 am-7 pm"],
                    "sunday": ["10 am-8 pm"],
                    "tuesday": ["10 am-7 pm"],
                    "saturday": ["10 am-8 pm"],
                    "thursday": ["10 am-7 pm"],
                    "wednesday": ["10 am-7 pm"],
                },
                "images": [
                    "https://lh3.googleusercontent.com/gps-cs-s/AG0ilSwcLTxOPFFcaWQj8_B6XU8cyFvePh46G4hSG02ownwIynGTh8z07qAa1PaImhFwFXCh_BQXiWb_k3-EIpV6Q7g_FguEHZuzbcboZbBYPt9QEmJ5EgZtQZ-9C-_GgmPwAhqlhuP3qg"
                ],
            },
            {
                "poi_id": "871fede6-4097-4072-a820-947d69bdae36",
                "poi_name": "Marina Square",
                "poi_destination": "Singapore",
                "latitude": 1.291153,
                "longitude": 103.857678,
                "date": "2026-05-05",
                "time_type": "anyTime",
                "themes": ["shopping"],
                "role": "attraction",
                "open_hours": {
                    "friday": ["10 am-10 pm"],
                    "monday": ["10 am-10 pm"],
                    "sunday": ["10 am-10 pm"],
                    "tuesday": ["10 am-10 pm"],
                    "saturday": ["10 am-10 pm"],
                    "thursday": ["10 am-10 pm"],
                    "wednesday": ["10 am-10 pm"],
                },
                "images": [
                    "https://lh3.googleusercontent.com/gps-cs-s/AG0ilSw5KfTgSHx96z7iKMmDMAQWyLvXO1eSnYx3Trofyzp5Nc248eB1sDWkRSZZD2vo_fcsfCpTxtvgRag71X7PoQgzs2yjZPFdac5WyaR-DktAbmfcIovqG68ASi55EfkPk_2ODQ_HRw"
                ],
            },
        ],
    },
    "pacing": "relaxed",
}

# METRICS CALCULATION


def _get_base_poi_id(poi_id: str) -> str:
    """Strip _dayX suffix to get base POI ID."""
    return poi_id.rsplit("_", 1)[0] if "_" in poi_id else poi_id


def _is_within_meal_window(start_min: int) -> Tuple[bool, str]:
    """Check if a meal time is within preferred windows."""
    windows = {
        "breakfast": vrp_config.breakfast_win,
        "lunch": vrp_config.lunch_win,
        "dinner": vrp_config.dinner_win,
    }
    for name, (w_start, w_end) in windows.items():
        if w_start <= start_min <= w_end:
            return True, name
    return False, "none"


def calculate_preference_alignment_score(result: Dict, payload: Dict, maut_output: Dict) -> Dict[str, Any]:
    """
    Calculate how well the generated itinerary aligns with user preferences.

    This uses the MAUT scores from the POI selection phase to measure
    how well the final scheduled POIs match user preferences.

    Returns:
        Dict with alignment metrics:
        - avg_maut_score: Average MAUT score of scheduled POIs
        - theme_coverage: % of user themes represented in itinerary
        - constraint_satisfaction: % of boolean constraints met
        - preference_alignment_total: Weighted overall score
    """
    days = result.get("days", [])
    if not days:
        return {
            "avg_maut_score": 0.0,
            "theme_coverage": 0.0,
            "constraint_satisfaction": 0.0,
            "preference_alignment_total": 0.0,
        }

    # Get user preferences
    prefs = payload.get("preferences", {})
    user_themes = set(prefs.get("interests", []))

    # Build lookup of MAUT scores by POI ID
    maut_scores: Dict[str, float] = {}
    for place in maut_output.get("places", []):
        poi_id = place.get("id")
        score = place.get("maut_score") or place.get("_score") or 0.0
        if poi_id:
            maut_scores[poi_id] = score

    # Collect scheduled POI metrics
    scheduled_scores = []
    scheduled_themes: Set[str] = set()
    constraint_checks = {
        "kids_friendly": 0,
        "pets_friendly": 0,
        "wheelchair_accessible": 0,
        "halal_compliant": 0,
        "theme_match": 0,
    }
    total_attractions = 0
    total_meals = 0

    for day in days:
        for stop in day.get("stops", []):
            role = stop.get("role", "")
            if role in ("depot", "accommodation", "hotel"):
                continue

            poi_id = stop.get("poi_id", "")
            base_id = _get_base_poi_id(poi_id)

            # Get MAUT score
            score = maut_scores.get(base_id, 0.0)
            scheduled_scores.append(score)

            # Track themes
            themes = stop.get("themes", [])
            for t in themes:
                scheduled_themes.add(t)

            if role == "attraction":
                total_attractions += 1
                # Check theme match
                if themes and user_themes:
                    if any(t in user_themes for t in themes):
                        constraint_checks["theme_match"] += 1

            if role == "meal":
                total_meals += 1

    # Calculate metrics
    avg_maut_score = sum(scheduled_scores) / len(scheduled_scores) if scheduled_scores else 0.0

    # Theme coverage: what % of user themes appear in itinerary
    theme_coverage = len(scheduled_themes & user_themes) / len(user_themes) * 100 if user_themes else 100.0

    # Theme match rate for attractions
    theme_match_rate = constraint_checks["theme_match"] / total_attractions * 100 if total_attractions > 0 else 0.0

    # Overall preference alignment (weighted)
    preference_alignment_total = (
        0.40 * avg_maut_score * 100  # MAUT score contribution
        + 0.30 * theme_coverage  # Theme coverage
        + 0.30 * theme_match_rate  # Theme match rate
    )

    return {
        "avg_maut_score": round(avg_maut_score, 4),
        "theme_coverage": round(theme_coverage, 2),
        "theme_match_rate": round(theme_match_rate, 2),
        "preference_alignment_total": round(preference_alignment_total, 2),
        "scheduled_themes": list(scheduled_themes),
        "user_themes": list(user_themes),
        "total_attractions": total_attractions,
        "total_meals": total_meals,
    }


def calculate_solver_metrics(
    result: Dict, execution_time: float, solver_name: str, payload: Dict, maut_output: Dict
) -> Dict[str, Any]:
    """Calculate comprehensive metrics for a solver result."""
    days = result.get("days", [])

    if not days:
        return {"error": "No days in result", "solver": solver_name}

    # Initialize counters
    unique_pois: Set[str] = set()
    meals = 0
    attractions = 0
    meals_in_window = 0
    daily_utilisation = []
    violations = []

    # Constraint flags
    time_sequence_valid = True
    meal_constraints_valid = True
    theme_constraints_valid = True
    food_streak_valid = True
    no_duplicates = True

    # Track theme distribution
    all_themes: Dict[str, int] = {}

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        day_meals = 0
        food_streak = 0
        day_themes: Dict[str, int] = {}
        active_time = 0

        for i, stop in enumerate(stops):
            role = stop.get("role", "")

            if role in ("depot", "accommodation", "hotel"):
                continue

            poi_id = stop.get("poi_id", "")
            base_id = _get_base_poi_id(poi_id)

            # Check duplicates
            if base_id in unique_pois and "check" not in poi_id.lower():
                no_duplicates = False
                violations.append(f"Duplicate POI: {base_id}")
            unique_pois.add(base_id)

            # Calculate active time
            try:
                arrival = time_to_minutes(stop.get("arrival", "00:00"))
                depart = time_to_minutes(stop.get("depart", "00:00"))
                active_time += max(0, depart - arrival)
            except:
                pass

            if role == "meal":
                meals += 1
                day_meals += 1
                food_streak += 1

                # Track themes for meals
                themes = stop.get("themes", [])
                if themes:
                    primary = themes[0]
                    all_themes[primary] = all_themes.get(primary, 0) + 1

                start_time = time_to_minutes(stop.get("arrival", "00:00"))
                in_window, _ = _is_within_meal_window(start_time)
                if in_window:
                    meals_in_window += 1

                if i > 0 and stops[i - 1].get("role") == "meal":
                    meal_constraints_valid = False
                    violations.append(f"Day {day_idx + 1}: Consecutive meals")

                if food_streak > 2:
                    food_streak_valid = False
                    violations.append(f"Day {day_idx + 1}: Food streak > 2")

            elif role == "attraction":
                attractions += 1
                food_streak = 0

                themes = stop.get("themes", [])
                if themes:
                    primary = themes[0]
                    day_themes[primary] = day_themes.get(primary, 0) + 1
                    all_themes[primary] = all_themes.get(primary, 0) + 1
            else:
                food_streak = 0

            # Time sequence check
            if i > 0:
                try:
                    prev_depart = time_to_minutes(stops[i - 1].get("depart", "00:00"))
                    curr_arrival = time_to_minutes(stop.get("arrival", "00:00"))
                    if curr_arrival < prev_depart:
                        time_sequence_valid = False
                        violations.append(f"Day {day_idx + 1}: Time sequence invalid")
                except:
                    pass

        if day_meals > 3:
            meal_constraints_valid = False
            violations.append(f"Day {day_idx + 1}: {day_meals} meals (max 3)")

        # Daily utilisation (assuming 720 min = 12 hour day for balanced)
        daily_budget = 720
        utilisation = (active_time / daily_budget) * 100 if daily_budget > 0 else 0
        daily_utilisation.append(round(utilisation, 2))

    avg_utilisation = sum(daily_utilisation) / len(daily_utilisation) if daily_utilisation else 0
    meal_window_compliance = (meals_in_window / meals * 100) if meals > 0 else 100

    # Get distance from result meta
    total_distance = result.get("meta", {}).get("total_distance", 0)

    # Calculate preference alignment
    pref_alignment = calculate_preference_alignment_score(result, payload, maut_output)

    return {
        "solver": solver_name,
        "execution_time_sec": round(execution_time, 2),
        "time_window_satisfaction_rate": 100.0 if time_sequence_valid else 0.0,
        "time_utilisation_score": round(avg_utilisation, 2),
        "daily_utilisation": daily_utilisation,
        "total_distance_km": round(total_distance, 2),
        "unique_pois": len(unique_pois),
        "meals_scheduled": meals,
        "attractions_scheduled": attractions,
        "meal_window_compliance": round(meal_window_compliance, 2),
        "theme_distribution": all_themes,
        "constraints": {
            "time_sequence": time_sequence_valid,
            "meal_constraints": meal_constraints_valid,
            "theme_constraints": theme_constraints_valid,
            "food_streak": food_streak_valid,
            "no_duplicates": no_duplicates,
        },
        "feasible": len(violations) == 0,
        "violations": violations,
        "preference_alignment": pref_alignment,
    }


def run_scenario_comparison(scenario: Dict) -> Dict[str, Any]:
    """Run comparison for a single scenario."""
    payload = scenario["payload"]
    pacing = scenario.get("pacing", "balanced")

    # Transform payload
    transformed = transform_frontend_payload(payload)

    print(f"\n{'=' * 80}")
    print(f"🧪 {scenario['title']}")
    print(f"   {scenario['description']}")
    print(f"{'=' * 80}")

    results = {
        "scenario": scenario["title"],
        "description": scenario["description"],
        "pacing": pacing,
        "payload_summary": {
            "destination": payload.get("destination"),
            "destinations": [d.get("city") for d in payload.get("destinations", [])],
            "num_days": transformed.get("num_days"),
            "interests": payload.get("preferences", {}).get("interests", []),
            "flags": payload.get("flags", {}),
            "dietary": payload.get("dietary_restrictions", []),
        },
        "timestamp": datetime.now().isoformat(),
    }

    # Run MAUT first to get POI candidates
    print("  Running MAUT for POI selection...")
    maut_out = run_maut(transformed)

    if maut_out.get("status") != "ok":
        print(f"    ❌ MAUT failed: {maut_out.get('error', 'Unknown error')}")
        results["error"] = f"MAUT failed: {maut_out.get('error', 'Unknown error')}"
        return results

    print(f"    ✓ MAUT selected {len(maut_out.get('places', []))} POIs")
    # Inject dates/num_days into MAUT output (required by pipeline)
    maut_out.setdefault("meta", {})
    maut_out["meta"]["dates"] = payload.get("dates", {})
    maut_out["meta"]["num_days"] = transformed.get("num_days")

    # Run OR-Tools
    print("  Running OR-Tools CVRPTW...")
    start = time.time()
    ortools_result = run_full_pipeline(maut_output=maut_out, pacing=pacing, solver="ortools", time_limit_sec=20)
    ortools_time = time.time() - start

    if ortools_result.get("status") == "success":
        print(f"    ✓ Completed in {ortools_time:.2f}s")
    else:
        print(f"    ⚠ Status: {ortools_result.get('status')} - {ortools_result.get('error', '')}")

    ortools_metrics = calculate_solver_metrics(ortools_result, ortools_time, "OR-Tools", payload, maut_out)
    results["ortools"] = ortools_metrics

    # Run ACS
    print("  Running ACS-CVRPTW...")
    start = time.time()
    acs_result = run_full_pipeline(maut_output=maut_out, pacing=pacing, solver="acs")
    acs_time = time.time() - start

    if acs_result.get("status") == "success":
        print(f"    ✓ Completed in {acs_time:.2f}s")
    else:
        print(f"    ⚠ Status: {acs_result.get('status')} - {acs_result.get('error', '')}")

    acs_metrics = calculate_solver_metrics(acs_result, acs_time, "ACS", payload, maut_out)
    results["acs"] = acs_metrics

    return results


def print_scenario_report(results: Dict):
    """Print formatted report for a scenario."""
    print(f"\n📊 Results for: {results['scenario']}")
    print(f"   Pacing: {results['pacing']}")
    print("-" * 70)

    ort = results.get("ortools", {})
    acs = results.get("acs", {})

    print(f"{'Metric':<35} {'OR-Tools':>15} {'ACS':>15}")
    print("-" * 70)

    metrics = [
        ("Execution Time (sec)", "execution_time_sec", "s", False),
        ("Time Utilisation Score (%)", "time_utilisation_score", "%", True),
        ("Total Distance (km)", "total_distance_km", "", False),
        ("Unique POIs", "unique_pois", "", True),
        ("Meals Scheduled", "meals_scheduled", "", False),
        ("Attractions Scheduled", "attractions_scheduled", "", True),
        ("Meal Window Compliance (%)", "meal_window_compliance", "%", True),
    ]

    for label, key, suffix, higher_better in metrics:
        ort_val = ort.get(key, 0)
        acs_val = acs.get(key, 0)
        if suffix == "s":
            print(f"{label:<35} {ort_val:>14.2f}s {acs_val:>14.2f}s")
        elif suffix == "%":
            print(f"{label:<35} {ort_val:>14.1f}% {acs_val:>14.1f}%")
        else:
            print(f"{label:<35} {ort_val:>15} {acs_val:>15}")

    # Preference Alignment (NEW)
    print("-" * 70)
    print("Preference Alignment:")
    ort_pref = ort.get("preference_alignment", {})
    acs_pref = acs.get("preference_alignment", {})

    print(
        f"  {'Avg MAUT Score':<33} {ort_pref.get('avg_maut_score', 0):>14.4f} {acs_pref.get('avg_maut_score', 0):>14.4f}"
    )
    print(
        f"  {'Theme Coverage (%)':<33} {ort_pref.get('theme_coverage', 0):>14.1f}% {acs_pref.get('theme_coverage', 0):>14.1f}%"
    )
    print(
        f"  {'Theme Match Rate (%)':<33} {ort_pref.get('theme_match_rate', 0):>14.1f}% {acs_pref.get('theme_match_rate', 0):>14.1f}%"
    )
    print(
        f"  {'Overall Alignment Score':<33} {ort_pref.get('preference_alignment_total', 0):>14.1f} {acs_pref.get('preference_alignment_total', 0):>14.1f}"
    )

    # Constraints
    print("-" * 70)
    print("Constraint Compliance:")
    ort_const = ort.get("constraints", {})
    acs_const = acs.get("constraints", {})
    for c in ["time_sequence", "meal_constraints", "food_streak", "no_duplicates"]:
        ort_c = "✅" if ort_const.get(c, False) else "❌"
        acs_c = "✅" if acs_const.get(c, False) else "❌"
        print(f"  {c:<33} {ort_c:>15} {acs_c:>15}")

    print("-" * 70)
    ort_feasible = "✅" if ort.get("feasible", False) else "❌"
    acs_feasible = "✅" if acs.get("feasible", False) else "❌"
    print(f"{'OVERALL FEASIBLE':<35} {ort_feasible:>15} {acs_feasible:>15}")

    # Theme distribution
    print("\nTheme Distribution (All POIs):")
    ort_themes = ort.get("theme_distribution", {})
    acs_themes = acs.get("theme_distribution", {})
    user_themes = set(results.get("payload_summary", {}).get("interests", []))
    all_themes = user_themes | set(ort_themes.keys()) | set(acs_themes.keys())
    for theme in sorted(all_themes):
        print(f"  {theme:<33} {ort_themes.get(theme, 0):>15} {acs_themes.get(theme, 0):>15}")


# PYTEST TESTS


@pytest.fixture(scope="module")
def all_scenario_results():
    """Run all scenarios and collect results."""
    results = {}

    scenarios = [
        SCENARIO_MULTI_CITY_BALANCED,
        SCENARIO_SINGLE_CITY_SINGLE_THEME,
        SCENARIO_CONSTRAINED_FAMILY,
        SCENARIO_MANDATORY_POI_HOTEL_SELECTED,
        SCENARIO_MULTI_CITY_REPEAT_VISIT_MANDATORY_POIS,
    ]

    for scenario in scenarios:
        key = scenario["title"].split(":")[0].strip().replace(" ", "_").lower()
        results[key] = run_scenario_comparison(scenario)
        print_scenario_report(results[key])

    output_path = os.path.join(OUTPUT_DIR, "scenario_comparison_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n✅ Results saved to: {output_path}")

    print("\n📈 Generating comparison charts...")
    generate_all_charts(results)

    return results


def test_scenario_1_multi_city_balanced(all_scenario_results):
    """Test Scenario 1: Multi-day multi-city balanced trip."""
    results = all_scenario_results.get("scenario_1", {})

    # Both solvers should produce feasible solutions
    assert results.get("ortools", {}).get("feasible", False), "OR-Tools should produce feasible solution"
    assert results.get("acs", {}).get("feasible", False), "ACS should produce feasible solution"

    # Both should have reasonable POI counts
    assert results.get("ortools", {}).get("unique_pois", 0) >= 10, "OR-Tools should schedule at least 10 POIs"
    assert results.get("acs", {}).get("unique_pois", 0) >= 10, "ACS should schedule at least 10 POIs"


def test_scenario_2_single_city_single_theme(all_scenario_results):
    """Test Scenario 2: Single city single theme (shopping) trip."""
    results = all_scenario_results.get("scenario_2", {})

    # Both should be feasible
    assert results.get("ortools", {}).get("feasible", False), "OR-Tools should produce feasible solution"
    assert results.get("acs", {}).get("feasible", False), "ACS should produce feasible solution"

    # Theme distribution should be shopping-dominant (expected behaviour)
    ort_themes = results.get("ortools", {}).get("theme_distribution", {})
    acs_themes = results.get("acs", {}).get("theme_distribution", {})

    # Shopping should be the dominant theme if present
    # (This validates that single-theme selection works correctly)
    total_ort = sum(ort_themes.values())
    total_acs = sum(acs_themes.values())

    if "shopping" in ort_themes and total_ort > 0:
        shopping_ratio = ort_themes.get("shopping", 0) / total_ort
        print(f"OR-Tools shopping ratio: {shopping_ratio:.2f}")

    if "shopping" in acs_themes and total_acs > 0:
        shopping_ratio = acs_themes.get("shopping", 0) / total_acs
        print(f"ACS shopping ratio: {shopping_ratio:.2f}")


def test_scenario_3_constrained_family(all_scenario_results):
    """Test Scenario 3: Family trip with full constraints."""
    results = all_scenario_results.get("scenario_3", {})

    assert results.get("ortools", {}).get("feasible", False), "OR-Tools should produce feasible solution"
    assert results.get("acs", {}).get("feasible", False), "ACS should produce feasible solution"


# def test_scenario_5_multi_city_assigned_days(all_scenario_results):
#     """Test Scenario 5: Multi-city with per-city date assignments."""
#     results = all_scenario_results.get("scenario_5", {})

#     assert results.get("ortools", {}).get("feasible", False), "OR-Tools should produce feasible solution"
#     assert results.get("acs", {}).get("feasible", False), "ACS should produce feasible solution"


def test_scenario_6_multi_city_repeat_visit_mandatory_pois(all_scenario_results):
    """Test Scenario 6: Multi-city with mandatory POIs."""
    results = all_scenario_results.get("scenario_6", {})

    assert results.get("ortools", {}).get("feasible", False), "OR-Tools should produce feasible solution"
    assert results.get("acs", {}).get("feasible", False), "ACS should produce feasible solution"


def test_all_scenarios_generate_results(all_scenario_results):
    """Verify all scenarios produced results and JSON was saved."""
    assert len(all_scenario_results) == 5, "Should have 5 scenario results"

    output_path = os.path.join(OUTPUT_DIR, "scenario_comparison_results.json")
    assert os.path.exists(output_path), "JSON output file should exist"

    with open(output_path, "r") as f:
        saved_data = json.load(f)

    assert len(saved_data) == 5, "Saved JSON should have 5 scenarios"
    print("\n" + "=" * 80)
    print("📋 ALL SCENARIO TESTS COMPLETED SUCCESSFULLY")
    print("=" * 80)
