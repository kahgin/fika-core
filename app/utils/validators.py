from typing import Dict, List, Any
import datetime as dt


# Configuration

MEAL_WINDOWS = {
    "breakfast": (7 * 60, 10 * 60),  # 7am-10am
    "lunch": (12 * 60, 14 * 60),  # 12pm-2pm
    "dinner": (18 * 60, 21 * 60),  # 6pm-9pm
}

DEFAULT_HOURS = {
    "nature": (0, 24 * 60),  # 24/7 for nature & parks
    "meal": (10 * 60, 22 * 60),  # 10am-10pm for meals
    "attraction": (10 * 60, 22 * 60),  # 10am-10pm for attractions
    "24h": (0, 24 * 60),  # 24/7 for explicitly marked
}

MAX_DAY_OVERRUN_MIN = 60  # Allow 1 hour past day end


# Helper Functions


def time_to_minutes(time_str: str) -> int:
    """Convert 'HH:MM' to minutes from midnight."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def get_meal_type(arrival_min: int) -> str:
    """Determine meal type based on arrival time."""
    if MEAL_WINDOWS["breakfast"][0] <= arrival_min <= MEAL_WINDOWS["breakfast"][1]:
        return "breakfast"
    elif MEAL_WINDOWS["lunch"][0] <= arrival_min <= MEAL_WINDOWS["lunch"][1]:
        return "lunch"
    elif MEAL_WINDOWS["dinner"][0] <= arrival_min <= MEAL_WINDOWS["dinner"][1]:
        return "dinner"
    else:
        return "other"


# Validation Functions


def validate_itinerary(
    cvrptw_output: Dict[str, Any], maut_output: Dict[str, Any], pacing: str = "balanced"
) -> Dict[str, Any]:
    """
    Validate CVRPTW output against business rules.

    Returns:
        {
            "valid": bool,
            "violations": [{"type": str, "severity": str, "message": str, "day": int, "poi": str}],
            "stats": {...}
        }
    """
    violations = []
    stats = {
        "total_days": len(cvrptw_output.get("days", [])),
        "total_stops": 0,
        "total_meals": 0,
        "meals_per_day": [],
        "theme_distribution": {},
        "day_overruns": [],
    }

    # Get POI details from MAUT output
    poi_lookup = {p["id"]: p for p in maut_output.get("places", [])}

    # Day end times based on pacing
    day_end_times = {
        "relaxed": 18 * 60,  # 6pm
        "balanced": 20 * 60,  # 8pm
        "packed": 22 * 60,  # 10pm
    }
    day_end = day_end_times.get(pacing, 20 * 60)

    for day_idx, day in enumerate(cvrptw_output.get("days", [])):
        day_num = day_idx + 1
        stops = day.get("stops", [])
        meals_today = 0
        prev_stop = None

        stats["total_stops"] += len([s for s in stops if s["role"] != "hotel"])

        for stop_idx, stop in enumerate(stops):
            poi_id_base = stop["poi_id"].rsplit("_day", 1)[0]
            poi = poi_lookup.get(poi_id_base)

            arrival_min = time_to_minutes(stop["arrival"])
            depart_min = time_to_minutes(stop["depart"])

            # Skip hotel stops for most checks
            if stop["role"] == "hotel":
                # Check day overrun
                if arrival_min > day_end + MAX_DAY_OVERRUN_MIN:
                    overrun = arrival_min - day_end
                    violations.append(
                        {
                            "type": "day_overrun",
                            "severity": "warning",
                            "message": f"Day {day_num} ends {overrun} min past limit ({stop['arrival']})",
                            "day": day_num,
                            "poi": stop["name"],
                            "overrun_minutes": overrun,
                        }
                    )
                    stats["day_overruns"].append(overrun)
                continue

            # 1. Check consecutive meals
            if prev_stop and prev_stop["role"] == "meal" and stop["role"] == "meal":
                violations.append(
                    {
                        "type": "consecutive_meals",
                        "severity": "error",
                        "message": f"Consecutive meals ({prev_stop['name']} → {stop['name']})",
                        "day": day_num,
                        "poi": stop["name"],
                    }
                )

            # 2. Check meal timing
            if stop["role"] == "meal":
                meals_today += 1
                meal_type = get_meal_type(arrival_min)

                if meal_type == "other":
                    violations.append(
                        {
                            "type": "meal_timing",
                            "severity": "warning",
                            "message": f"Meal at unusual time ({stop['arrival']}) - {stop['name']}",
                            "day": day_num,
                            "poi": stop["name"],
                            "arrival": stop["arrival"],
                        }
                    )

            # 3. Check POI opening hours
            if poi and stop["role"] != "hotel":
                open_hours = poi.get("openHours")
                themes = poi.get("themes", [])

                # Determine expected hours
                if not open_hours:
                    # Default hours based on POI type
                    if "nature" in themes:
                        expected_hours = DEFAULT_HOURS["nature"]
                    elif stop["role"] == "meal":
                        expected_hours = DEFAULT_HOURS["meal"]
                    else:
                        expected_hours = DEFAULT_HOURS["attraction"]
                else:
                    # Parse actual hours for the day
                    date_str = day["date"]
                    date_obj = dt.date.fromisoformat(date_str)
                    weekday = date_obj.strftime("%A")

                    day_hours = open_hours.get(weekday, [])
                    if not day_hours:
                        # No hours specified, use default
                        expected_hours = DEFAULT_HOURS["attraction"]
                    elif "closed" in str(day_hours).lower():
                        violations.append(
                            {
                                "type": "poi_closed",
                                "severity": "error",
                                "message": f"POI closed on {weekday} - {stop['name']}",
                                "day": day_num,
                                "poi": stop["name"],
                            }
                        )
                        prev_stop = stop
                        continue
                    elif "open 24 hours" in str(day_hours).lower():
                        # Open 24 hours - skip validation
                        expected_hours = (0, 24 * 60)
                    else:
                        # For simplicity, use first time range
                        # TODO: Parse actual time ranges
                        expected_hours = DEFAULT_HOURS["attraction"]

                # Check if visit is within hours (skip if 24h)
                open_start, open_end = expected_hours
                if not (open_start == 0 and open_end == 24 * 60):
                    if arrival_min < open_start or depart_min > open_end:
                        violations.append(
                            {
                                "type": "outside_hours",
                                "severity": "warning",
                                "message": f"Visit outside hours ({stop['arrival']}-{stop['depart']}) - {stop['name']}",
                                "day": day_num,
                                "poi": stop["name"],
                                "expected_hours": f"{open_start // 60:02d}:{open_start % 60:02d}-{open_end // 60:02d}:{open_end % 60:02d}",
                            }
                        )

            # Track themes
            if poi:
                themes = poi.get("themes", [])
                for theme in themes:
                    stats["theme_distribution"][theme] = (
                        stats["theme_distribution"].get(theme, 0) + 1
                    )

            prev_stop = stop

        stats["meals_per_day"].append(meals_today)
        stats["total_meals"] += meals_today

    # 4. Check meals per day
    for day_idx, meal_count in enumerate(stats["meals_per_day"]):
        if meal_count < 1:
            violations.append(
                {
                    "type": "insufficient_meals",
                    "severity": "error",
                    "message": f"Day {day_idx + 1}: Only {meal_count} meals",
                    "day": day_idx + 1,
                    "poi": None,
                }
            )
        elif meal_count > 3:
            violations.append(
                {
                    "type": "excessive_meals",
                    "severity": "warning",
                    "message": f"Day {day_idx + 1}: {meal_count} meals (max 3 recommended)",
                    "day": day_idx + 1,
                    "poi": None,
                }
            )

    # 5. Check theme balance
    selected_themes = maut_output.get("meta", {}).get("selected_themes", [])
    if selected_themes:
        missing_themes = [
            t for t in selected_themes if stats["theme_distribution"].get(t, 0) == 0
        ]
        if missing_themes:
            violations.append(
                {
                    "type": "theme_imbalance",
                    "severity": "warning",
                    "message": f"Missing themes in itinerary: {', '.join(missing_themes)}",
                    "day": None,
                    "poi": None,
                    "missing_themes": missing_themes,
                }
            )

    return {
        "valid": len([v for v in violations if v["severity"] == "error"]) == 0,
        "violations": violations,
        "stats": stats,
    }


def print_validation_report(validation_result: Dict[str, Any]) -> None:
    """Print human-readable validation report."""
    print("\n" + "=" * 70)
    print("ITINERARY VALIDATION REPORT")
    print("=" * 70)

    stats = validation_result["stats"]
    print("\n📊 Statistics:")
    print(f"   Total days: {stats['total_days']}")
    print(f"   Total stops: {stats['total_stops']}")
    print(f"   Total meals: {stats['total_meals']}")
    print(f"   Meals per day: {stats['meals_per_day']}")

    if stats["theme_distribution"]:
        print("\n🎨 Theme Distribution:")
        for theme, count in sorted(
            stats["theme_distribution"].items(), key=lambda x: -x[1]
        ):
            print(f"   {theme}: {count}")

    violations = validation_result["violations"]
    if not violations:
        print("\n✅ VALID - No violations found")
    else:
        errors = [v for v in violations if v["severity"] == "error"]
        warnings = [v for v in violations if v["severity"] == "warning"]

        print(f"\n⚠️  Found {len(errors)} errors, {len(warnings)} warnings")

        if errors:
            print("\n❌ ERRORS:")
            for v in errors:
                day_str = f"Day {v['day']}: " if v["day"] else ""
                print(f"   {day_str}{v['message']}")

        if warnings:
            print("\n⚠️  WARNINGS:")
            for v in warnings:
                day_str = f"Day {v['day']}: " if v["day"] else ""
                print(f"   {day_str}{v['message']}")

    print("=" * 70 + "\n")


def assert_itinerary_valid(
    cvrptw_output: Dict[str, Any],
    maut_output: Dict[str, Any],
    pacing: str = "balanced",
    allow_warnings: bool = True,
) -> None:
    """
    Assert itinerary is valid, raise AssertionError if not.

    Args:
        allow_warnings: If False, warnings also cause assertion failure
    """
    result = validate_itinerary(cvrptw_output, maut_output, pacing)
    print_validation_report(result)

    errors = [v for v in result["violations"] if v["severity"] == "error"]
    warnings = [v for v in result["violations"] if v["severity"] == "warning"]

    if errors:
        error_msgs = [v["message"] for v in errors]
        raise AssertionError(
            f"Itinerary has {len(errors)} errors:\n"
            + "\n".join(f"  - {m}" for m in error_msgs)
        )

    if not allow_warnings and warnings:
        warning_msgs = [v["message"] for v in warnings]
        raise AssertionError(
            f"Itinerary has {len(warnings)} warnings:\n"
            + "\n".join(f"  - {m}" for m in warning_msgs)
        )
