from typing import Dict, Any, List, Optional, Tuple
import datetime as dt

from app.services.vrp_utils import (
    get_effective_windows,
    is_poi_open_on_date,
)
from app.utils.date_utils import time_to_minutes

# Configuration

MEAL_WINDOWS = {
    "breakfast": (7 * 60, 10 * 60),  # 07:00–10:00
    "lunch": (12 * 60, 14 * 60),  # 12:00–14:00
    "dinner": (18 * 60, 21 * 60),  # 18:00–21:00
}

DEFAULT_HOURS = {
    "nature": (0, 24 * 60),  # 24/7 for nature & parks
    "meal": (10 * 60, 22 * 60),  # 10:00–22:00
    "attraction": (10 * 60, 22 * 60),  # 10:00–22:00
    "24h": (0, 24 * 60),
}

MAX_DAY_OVERRUN_MIN = 60  # Allow 1 hour past day end

# Helpers


def get_meal_type(arrival_min: int) -> str:
    """Determine meal type based on arrival time."""
    if MEAL_WINDOWS["breakfast"][0] <= arrival_min <= MEAL_WINDOWS["breakfast"][1]:
        return "breakfast"
    if MEAL_WINDOWS["lunch"][0] <= arrival_min <= MEAL_WINDOWS["lunch"][1]:
        return "lunch"
    if MEAL_WINDOWS["dinner"][0] <= arrival_min <= MEAL_WINDOWS["dinner"][1]:
        return "dinner"
    return "other"


def get_default_window_for_role(
    role: str, themes: Optional[List[str]] = None
) -> Tuple[int, int]:
    """Get default time window based on POI role and themes."""
    if themes and "nature" in themes:
        return DEFAULT_HOURS["nature"]
    if role == "meal":
        return DEFAULT_HOURS["meal"]
    return DEFAULT_HOURS["attraction"]


def get_open_windows_for_date(
    poi: Dict[str, Any],
    date_obj: dt.date,
    role: str,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """
    Compute effective opening windows for a POI on a given date.

    Uses vrp_utils functions for parsing to ensure consistency with scheduling.
    Internal data uses snake_case (open_hours).

    Returns (is_explicitly_closed, windows).

    - If explicitly closed → (True, []).
    - If open 24 hours → (False, [(0, 1440)]).
    - If open_hours absent or unparseable → default windows by role/theme.
    - If multiple intervals → returns all of them.
    """
    # Internal data uses snake_case
    open_hours = poi.get("open_hours")
    themes = poi.get("themes", [])
    default_window = get_default_window_for_role(role, themes)

    # Use vrp_utils for consistent parsing
    is_open, intervals = is_poi_open_on_date(open_hours, date_obj)

    if not is_open:
        return (True, [])  # Explicitly closed

    if intervals:
        # Check for 24h
        if (0, 24 * 60) in intervals:
            return (False, [(0, 24 * 60)])
        return (False, intervals)

    # No intervals found, use defaults
    return (False, [default_window])


def is_within_any_window(
    arrival_min: int, depart_min: int, windows: List[Tuple[int, int]]
) -> bool:
    """Check if [arrival_min, depart_min] fits inside at least one (start,end) window."""
    for start, end in windows:
        if arrival_min >= start and depart_min <= end:
            return True
    return False


def validate_poi_schedule_against_hours(
    poi: Dict[str, Any],
    arrival_min: int,
    depart_min: int,
    date_obj: Optional[dt.date],
    role: str,
) -> Tuple[bool, str]:
    """
    Validate that a POI visit is within its opening hours.

    Args:
        poi: POI dict with open_hours (snake_case)
        arrival_min: Arrival time in minutes from midnight
        depart_min: Departure time in minutes from midnight
        date_obj: Date of visit (None for unknown-day itinerary)
        role: POI role (attraction, meal, etc.)

    Returns:
        (is_valid, error_message) - error_message is empty if valid
    """
    # Internal data uses snake_case
    open_hours = poi.get("open_hours")
    themes = poi.get("themes", [])
    default_window = get_default_window_for_role(role, themes)

    if date_obj is not None:
        # Date-specific validation
        is_open, intervals = is_poi_open_on_date(open_hours, date_obj)

        if not is_open:
            return (False, f"POI is closed on {date_obj.strftime('%A')}")

        windows = intervals if intervals else [default_window]
    else:
        # Unknown-day: use effective windows with representative interval
        is_open, windows = get_effective_windows(
            open_hours, None, default_window, use_representative=True
        )
        if not is_open:
            return (False, "POI is closed")

    # Check for 24h
    if (0, 24 * 60) in windows:
        return (True, "")

    if not is_within_any_window(arrival_min, depart_min, windows):
        if windows:
            s0, e0 = windows[0]
            expected = f"{s0 // 60:02d}:{s0 % 60:02d}-{e0 // 60:02d}:{e0 % 60:02d}"
        else:
            expected = "unknown"
        return (False, f"Visit outside hours, expected {expected}")

    return (True, "")


# Validation


def validate_itinerary(
    cvrptw_output: Dict[str, Any], maut_output: Dict[str, Any], pacing: str = "balanced"
) -> Dict[str, Any]:
    """
    Validate CVRPTW / ACS-CVRPTW output against business rules.

    Returns:
        {
            "valid": bool,
            "violations": [...],
            "stats": {...}
        }
    """
    violations: List[Dict[str, Any]] = []
    stats = {
        "total_days": len(cvrptw_output.get("days", [])),
        "total_stops": 0,
        "total_meals": 0,
        "meals_per_day": [],
        "theme_distribution": {},
        "day_overruns": [],
    }

    poi_lookup = {p["id"]: p for p in maut_output.get("places", [])}

    # Day end times based on pacing
    day_end_times = {
        "relaxed": 18 * 60,  # 18:00
        "balanced": 20 * 60,  # 20:00
        "packed": 22 * 60,  # 22:00
    }
    day_end_default = day_end_times.get(pacing, 20 * 60)

    days = cvrptw_output.get("days", [])

    for day_idx, day in enumerate(days):
        day_num = day_idx + 1
        date_str = day.get("date")
        date_obj = dt.date.fromisoformat(date_str) if date_str else None
        weekday_short = date_obj.strftime("%a") if date_obj else "?"

        stops = day.get("stops", [])
        meals_today = 0
        prev_stop = None

        stats["total_stops"] += len(
            [s for s in stops if s.get("role") not in ("hotel", "depot")]
        )

        for stop_idx, stop in enumerate(stops):
            poi_id_base = stop["poi_id"].rsplit("_day", 1)[0]
            poi = poi_lookup.get(poi_id_base)

            arrival_min = time_to_minutes(stop["arrival"])
            depart_min = time_to_minutes(stop["depart"])

            # Skip hotel/depot for most checks
            if stop.get("role") in ("hotel", "depot"):
                # Day overrun against pacing horizon
                if arrival_min > day_end_default + MAX_DAY_OVERRUN_MIN:
                    overrun = arrival_min - day_end_default
                    violations.append(
                        {
                            "type": "day_overrun",
                            "severity": "warning",
                            "message": (
                                f"Day {day_num} ({weekday_short}) ends "
                                f"{overrun} min past limit ({stop['arrival']})"
                            ),
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop.get("name"),
                            "overrun_minutes": overrun,
                        }
                    )
                    stats["day_overruns"].append(overrun)
                continue

            # 1. Consecutive meals
            if (
                prev_stop
                and prev_stop.get("role") == "meal"
                and stop.get("role") == "meal"
            ):
                violations.append(
                    {
                        "type": "consecutive_meals",
                        "severity": "error",
                        "message": (
                            f"Consecutive meals ({prev_stop.get('name')} → {stop.get('name')})"
                        ),
                        "day": day_num,
                        "weekday": weekday_short,
                        "poi": stop.get("name"),
                    }
                )

            # 2. Meal timing
            if stop.get("role") == "meal":
                meals_today += 1
                meal_type = get_meal_type(arrival_min)
                if meal_type == "other":
                    violations.append(
                        {
                            "type": "meal_timing",
                            "severity": "warning",
                            "message": (
                                f"Meal at unusual time ({stop['arrival']}) - "
                                f"{stop.get('name')}"
                            ),
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop.get("name"),
                            "arrival": stop["arrival"],
                        }
                    )

            # 3. Opening hours validation
            if poi and stop.get("role") not in ("hotel", "depot"):
                role = stop.get("role", "attraction")
                is_valid, error_msg = validate_poi_schedule_against_hours(
                    poi=poi,
                    arrival_min=arrival_min,
                    depart_min=depart_min,
                    date_obj=date_obj,
                    role=role,
                )

                if not is_valid:
                    if "closed" in error_msg.lower():
                        violations.append(
                            {
                                "type": "poi_closed",
                                "severity": "error",
                                "message": f"POI closed on {weekday_short} - {stop.get('name')}",
                                "day": day_num,
                                "weekday": weekday_short,
                                "poi": stop.get("name"),
                            }
                        )
                    else:
                        violations.append(
                            {
                                "type": "outside_hours",
                                "severity": "warning",
                                "message": (
                                    f"Visit outside hours "
                                    f"({stop['arrival']}-{stop['depart']}) - "
                                    f"{stop.get('name')}"
                                ),
                                "day": day_num,
                                "weekday": weekday_short,
                                "poi": stop.get("name"),
                                "expected_hours": error_msg.replace(
                                    "Visit outside hours, expected ", ""
                                ),
                            }
                        )
                    prev_stop = stop
                    continue

            # 4. Theme stats
            if poi:
                themes = poi.get("themes", [])
                for theme in themes:
                    stats["theme_distribution"][theme] = (
                        stats["theme_distribution"].get(theme, 0) + 1
                    )

            prev_stop = stop

        stats["meals_per_day"].append(meals_today)
        stats["total_meals"] += meals_today

    # 5. Meals per day constraints (global)
    # Note: Meal count is a soft constraint - depends on available meal POIs
    # that match dietary restrictions and time windows
    for day_idx, meal_count in enumerate(stats["meals_per_day"]):
        day_num = day_idx + 1
        if meal_count < 1:
            # Warning, not error - may be due to dietary restrictions limiting options
            violations.append(
                {
                    "type": "insufficient_meals",
                    "severity": "warning",
                    "message": f"Day {day_num}: Only {meal_count} meals (may be due to dietary restrictions)",
                    "day": day_num,
                    "weekday": None,
                    "poi": None,
                }
            )
        elif meal_count > 3:
            violations.append(
                {
                    "type": "excessive_meals",
                    "severity": "warning",
                    "message": f"Day {day_num}: {meal_count} meals (max 3 recommended)",
                    "day": day_num,
                    "weekday": None,
                    "poi": None,
                }
            )

    # 6. Theme balance against selected themes
    # Note: This is informational - theme coverage depends on available POIs
    # in the destination that match the selected themes
    selected_themes = maut_output.get("meta", {}).get("selected_themes", [])
    if selected_themes:
        missing_themes = [
            t for t in selected_themes if stats["theme_distribution"].get(t, 0) == 0
        ]
        if missing_themes:
            violations.append(
                {
                    "type": "theme_imbalance",
                    "severity": "info",
                    "message": (
                        "Missing themes in itinerary: "
                        + ", ".join(missing_themes)
                        + " (may not be available in destination)"
                    ),
                    "day": None,
                    "weekday": None,
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
        infos = [v for v in violations if v["severity"] == "info"]

        print(
            f"\n⚠️  Found {len(errors)} errors, {len(warnings)} warnings, {len(infos)} info"
        )

        if errors:
            print("\n❌ ERRORS:")
            for v in errors:
                day_str = ""
                if v.get("day") is not None:
                    if v.get("weekday"):
                        day_str = f"Day {v['day']} ({v['weekday']}): "
                    else:
                        day_str = f"Day {v['day']}: "
                print(f"   {day_str}{v['message']}")

        if warnings:
            print("\n⚠️  WARNINGS:")
            for v in warnings:
                day_str = ""
                if v.get("day") is not None:
                    if v.get("weekday"):
                        day_str = f"Day {v['day']} ({v['weekday']}): "
                    else:
                        day_str = f"Day {v['day']}: "
                print(f"   {day_str}{v['message']}")

        if infos:
            print("\nℹ️  INFO:")
            for v in infos:
                print(f"   {v['message']}")


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
