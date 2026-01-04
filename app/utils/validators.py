"""
Itinerary validation utilities.

Validates CVRPTW/ACS-CVRPTW output against business rules:
- No duplicate POIs across days
- Meal timing and constraints
- Opening hours
- Accommodation check-in/check-out pairing
- Day overruns
"""

from typing import Dict, Any, List, Optional, Tuple
import datetime as dt

from app.services.vrp_model import vrp_config
from app.services.vrp_utils import (
    get_effective_windows,
    is_poi_open_on_date,
)
from app.utils.date_utils import time_to_minutes

DEFAULT_HOURS = {
    "nature": (0, 24 * 60),
    "meal": vrp_config.default_role_windows.get("meal", (10 * 60, 22 * 60)),
    "attraction": vrp_config.default_role_windows.get("attraction", (10 * 60, 22 * 60)),
    "24h": (0, 24 * 60),
}

MAX_DAY_OVERRUN_MIN = 60


def get_meal_type(arrival_min: int) -> str:
    """Determine meal type based on arrival time."""
    if vrp_config.breakfast_win[0] <= arrival_min <= vrp_config.breakfast_win[1]:
        return "breakfast"
    if vrp_config.lunch_win[0] <= arrival_min <= vrp_config.lunch_win[1]:
        return "lunch"
    if vrp_config.dinner_win[0] <= arrival_min <= vrp_config.dinner_win[1]:
        return "dinner"
    return "other"


def get_default_window_for_role(role: str, themes: Optional[List[str]] = None) -> Tuple[int, int]:
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
    """Compute effective opening windows for a POI on a given date."""
    open_hours = poi.get("open_hours")
    themes = poi.get("themes", [])
    default_window = get_default_window_for_role(role, themes)

    is_open, intervals = is_poi_open_on_date(open_hours, date_obj)

    if not is_open:
        return (True, [])

    if intervals:
        if (0, 24 * 60) in intervals:
            return (False, [(0, 24 * 60)])
        return (False, intervals)

    return (False, [default_window])


def is_within_any_window(arrival_min: int, depart_min: int, windows: List[Tuple[int, int]]) -> bool:
    """Check if [arrival_min, depart_min] fits inside at least one window."""
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
    """Validate that a POI visit is within its opening hours."""
    open_hours = poi.get("open_hours")
    themes = poi.get("themes", [])
    default_window = get_default_window_for_role(role, themes)

    if date_obj is not None:
        is_open, intervals = is_poi_open_on_date(open_hours, date_obj)

        if not is_open:
            return (False, f"POI is closed on {date_obj.strftime('%A')}")

        windows = intervals if intervals else [default_window]
    else:
        is_open, windows = get_effective_windows(open_hours, None, default_window, use_representative=True)
        if not is_open:
            return (False, "POI is closed")

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


def validate_itinerary(
    cvrptw_output: Dict[str, Any], maut_output: Dict[str, Any], pacing: str = "balanced"
) -> Dict[str, Any]:
    """
    Validate CVRPTW/ACS-CVRPTW output against business rules.

    Checks:
    - No duplicate POIs across days
    - No consecutive meals
    - Meals within preferred time windows
    - POIs within opening hours
    - Max 3 meals per day
    - Day end time within pacing limits
    - Accommodation has paired check-in/check-out
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

    day_end_times = {
        "relaxed": 20 * 60,
        "balanced": 22 * 60,
        "packed": 22 * 60,
    }
    day_end_default = day_end_times.get(pacing, 20 * 60)

    days = cvrptw_output.get("days", [])

    # Track POIs across all days for duplicate detection
    seen_pois: Dict[str, int] = {}

    # Track accommodation events
    checkins: Dict[str, int] = {}
    checkouts: Dict[str, int] = {}
    stays: Dict[str, int] = {}
    hotel_events_per_day: Dict[int, List[str]] = {}  # day -> list of event types

    for day_idx, day in enumerate(days):
        day_num = day_idx + 1
        date_str = day.get("date")
        date_obj = dt.date.fromisoformat(date_str) if date_str else None
        weekday_short = date_obj.strftime("%a") if date_obj else "?"

        stops = day.get("stops", [])
        meals_today = 0
        prev_stop = None

        stats["total_stops"] += len([s for s in stops if s.get("role") not in ("hotel", "depot")])

        for stop_idx, stop in enumerate(stops):
            poi_id = stop.get("poi_id", "")
            role = stop.get("role", "attraction")
            poi = poi_lookup.get(poi_id)

            arrival_min = time_to_minutes(stop.get("arrival", "00:00"))
            depart_min = time_to_minutes(stop.get("depart", "00:00"))

            # Check for duplicate POIs (except accommodation which can have check-in and check-out)
            if role != "accommodation" and poi_id:
                if poi_id in seen_pois:
                    violations.append(
                        {
                            "type": "duplicate_poi",
                            "severity": "error",
                            "message": f"Duplicate: Day {day_num}: {stop.get('name')} ({poi_id})",
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop.get("name"),
                            "poi_id": poi_id,
                            "first_seen_day": seen_pois[poi_id],
                        }
                    )
                else:
                    seen_pois[poi_id] = day_num

            # Track accommodation events
            if role == "accommodation":
                hotel_event_type = stop.get("hotel_event_type")
                if hotel_event_type == "checkin":
                    checkins[poi_id] = day_num
                elif hotel_event_type == "checkout":
                    checkouts[poi_id] = day_num
                elif hotel_event_type == "stay":
                    stays[poi_id] = day_num

                # Track hotel events per day for single-event-per-day validation
                if hotel_event_type:
                    hotel_events_per_day.setdefault(day_num, []).append(hotel_event_type)

            # Skip depot for most checks
            if role in ("hotel", "depot"):
                if arrival_min > day_end_default + MAX_DAY_OVERRUN_MIN:
                    overrun = arrival_min - day_end_default
                    violations.append(
                        {
                            "type": "day_overrun",
                            "severity": "warning",
                            "message": f"Day {day_num} ({weekday_short}) ends {overrun} min past limit",
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop.get("name"),
                            "overrun_minutes": overrun,
                        }
                    )
                    stats["day_overruns"].append(overrun)
                continue

            # Consecutive meals check
            if prev_stop and prev_stop.get("role") == "meal" and role == "meal":
                violations.append(
                    {
                        "type": "consecutive_meals",
                        "severity": "error",
                        "message": f"Consecutive meals ({prev_stop.get('name')} → {stop.get('name')})",
                        "day": day_num,
                        "weekday": weekday_short,
                        "poi": stop.get("name"),
                    }
                )

            # Meal timing check
            if role == "meal":
                meals_today += 1
                meal_type = get_meal_type(arrival_min)
                if meal_type == "other":
                    violations.append(
                        {
                            "type": "meal_timing",
                            "severity": "warning",
                            "message": f"Meal at unusual time ({stop.get('arrival')}) - {stop.get('name')}",
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop.get("name"),
                            "arrival": stop.get("arrival"),
                        }
                    )

            # Opening hours validation
            if poi and role not in ("hotel", "depot", "accommodation"):
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
                                "message": f"Visit outside hours - {stop.get('name')}",
                                "day": day_num,
                                "weekday": weekday_short,
                                "poi": stop.get("name"),
                            }
                        )
                    prev_stop = stop
                    continue

            # Theme stats
            if poi:
                themes = poi.get("themes", [])
                for theme in themes:
                    stats["theme_distribution"][theme] = stats["theme_distribution"].get(theme, 0) + 1

            prev_stop = stop

        stats["meals_per_day"].append(meals_today)
        stats["total_meals"] += meals_today

    # Meals per day constraints
    for day_idx, meal_count in enumerate(stats["meals_per_day"]):
        day_num = day_idx + 1
        if meal_count < 1:
            violations.append(
                {
                    "type": "insufficient_meals",
                    "severity": "warning",
                    "message": f"Day {day_num}: Only {meal_count} meals",
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

    # Accommodation check-in/check-out pairing validation
    all_hotels = set(checkins.keys()) | set(checkouts.keys())
    for hotel_id in all_hotels:
        has_checkin = hotel_id in checkins
        has_checkout = hotel_id in checkouts

        if has_checkin and not has_checkout:
            violations.append(
                {
                    "type": "missing_checkout",
                    "severity": "warning",
                    "message": f"Hotel {hotel_id} has check-in on day {checkins[hotel_id]} but no check-out",
                    "day": checkins[hotel_id],
                    "weekday": None,
                    "poi": hotel_id,
                }
            )
        elif has_checkout and not has_checkin:
            violations.append(
                {
                    "type": "missing_checkin",
                    "severity": "warning",
                    "message": f"Hotel {hotel_id} has check-out on day {checkouts[hotel_id]} but no check-in",
                    "day": checkouts[hotel_id],
                    "weekday": None,
                    "poi": hotel_id,
                }
            )
        elif has_checkin and has_checkout:
            if checkouts[hotel_id] <= checkins[hotel_id]:
                violations.append(
                    {
                        "type": "invalid_hotel_sequence",
                        "severity": "error",
                        "message": f"Hotel {hotel_id}: check-out (day {checkouts[hotel_id]}) before/same as check-in (day {checkins[hotel_id]})",
                        "day": checkins[hotel_id],
                        "weekday": None,
                        "poi": hotel_id,
                    }
                )

    # Validate single hotel event per day (only one of checkin/checkout/stay per day)
    for day_num, events in hotel_events_per_day.items():
        # Filter to unique hotel event types (not counting duplicate stays)
        unique_events = list(set(events))
        # A day should have at most: checkin OR checkout OR stay
        # Exception: checkout + checkin is allowed on transition days
        if len(unique_events) > 1:
            if set(unique_events) == {"checkout", "checkin"}:
                # This is allowed for transition days
                pass
            elif "stay" in unique_events and len(unique_events) > 1:
                violations.append(
                    {
                        "type": "multiple_hotel_events",
                        "severity": "error",
                        "message": f"Day {day_num}: Multiple hotel events ({', '.join(unique_events)}) - stay should be alone",
                        "day": day_num,
                        "weekday": None,
                        "poi": None,
                    }
                )
            elif len(unique_events) > 2:
                violations.append(
                    {
                        "type": "multiple_hotel_events",
                        "severity": "error",
                        "message": f"Day {day_num}: Too many hotel events ({', '.join(unique_events)})",
                        "day": day_num,
                        "weekday": None,
                        "poi": None,
                    }
                )

    # Theme imbalance check - compare visited themes against user-selected themes
    selected_themes = maut_output.get("meta", {}).get("selected_themes", [])
    if selected_themes:
        visited_themes = set(stats["theme_distribution"].keys())
        missing_themes = [t for t in selected_themes if t not in visited_themes]
        if missing_themes:
            violations.append(
                {
                    "type": "theme_imbalance",
                    "severity": "info",
                    "message": f"User-selected themes not covered: {', '.join(missing_themes)}",
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
        for theme, count in sorted(stats["theme_distribution"].items(), key=lambda x: -x[1]):
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
                day_str = f"Day {v['day']}: " if v.get("day") else ""
                print(f"   {day_str}{v['message']}")

        if warnings:
            print("\n⚠️  WARNINGS:")
            for v in warnings:
                day_str = f"Day {v['day']}: " if v.get("day") else ""
                print(f"   {day_str}{v['message']}")
