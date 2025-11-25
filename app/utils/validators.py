from typing import Dict, Any, List, Optional, Tuple
import datetime as dt

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


def time_to_minutes(time_str: str) -> int:
    """Convert 'HH:MM' to minutes from midnight."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def get_meal_type(arrival_min: int) -> str:
    """Determine meal type based on arrival time."""
    if MEAL_WINDOWS["breakfast"][0] <= arrival_min <= MEAL_WINDOWS["breakfast"][1]:
        return "breakfast"
    if MEAL_WINDOWS["lunch"][0] <= arrival_min <= MEAL_WINDOWS["lunch"][1]:
        return "lunch"
    if MEAL_WINDOWS["dinner"][0] <= arrival_min <= MEAL_WINDOWS["dinner"][1]:
        return "dinner"
    return "other"


def parse_time_range_label(label: str) -> Optional[Tuple[int, int]]:
    """
    Parse '10 am-9 pm', '11:45 am-2:30 pm', '5:30-10 pm', 'open 24 hours', 'closed'.

    Returns (start_min, end_min) in [0, 1440], with overnight ranges
    (e.g. 8am–2am) clamped to (start, 24:00).
    """
    s = label.strip().lower()
    if "closed" in s:
        return None
    if "open 24 hours" in s:
        return (0, 24 * 60)

    try:
        left, right = [x.strip() for x in label.split("-", 1)]

        def to_min(x: str) -> int:
            x = x.strip().lower().replace(" ", "")
            ampm = "am" if "am" in x else "pm"
            hhmm = x.replace("am", "").replace("pm", "")
            if ":" in hhmm:
                h_str, m_str = hhmm.split(":", 1)
                h = int(h_str)
                m = int(m_str)
            else:
                h = int(hhmm)
                m = 0
            if ampm == "am":
                if h == 12:
                    h = 0
            else:
                if h != 12:
                    h += 12
            return h * 60 + m

        start = to_min(left)
        end = to_min(right)

        # Overnight like 8am–2am: clamp to midnight for this date
        if end <= start:
            end = 24 * 60

        return start, end
    except Exception:
        return None


def get_open_windows_for_date(
    poi: Dict[str, Any],
    date_obj: dt.date,
    role: str,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """
    Compute effective opening windows for a POI on a given date.

    Returns (is_explicitly_closed, windows).

    - If explicitly closed → (True, []).
    - If open 24 hours → (False, [(0, 1440)]).
    - If openHours absent or unparseable → default windows by role/theme.
    - If multiple intervals → returns all of them.
    """
    open_hours = poi.get("openHours")
    themes = poi.get("themes", [])
    weekday = date_obj.strftime("%A")

    # No openHours: use defaults
    if not open_hours or weekday not in open_hours:
        if "nature" in themes:
            return False, [DEFAULT_HOURS["nature"]]
        if role == "meal":
            return False, [DEFAULT_HOURS["meal"]]
        return False, [DEFAULT_HOURS["attraction"]]

    raw = open_hours.get(weekday) or []
    if not raw:
        # No entries for that weekday; treat as default attraction hours
        return False, [DEFAULT_HOURS["attraction"]]

    windows: List[Tuple[int, int]] = []
    saw_closed = False
    saw_24h = False

    # raw is expected to be a list of labels
    for label in raw:
        s = str(label).lower()
        if "closed" in s:
            saw_closed = True
            continue
        rng = parse_time_range_label(str(label))
        if not rng:
            continue
        if rng == (0, 24 * 60):
            saw_24h = True
        windows.append(rng)

    if saw_closed and not windows:
        # Explicitly closed all day
        return True, []

    if saw_24h:
        # At least one window is 24h → treat as full-day open
        return False, [(0, 24 * 60)]

    if not windows:
        # Fallback if everything failed
        if "nature" in themes:
            return False, [DEFAULT_HOURS["nature"]]
        if role == "meal":
            return False, [DEFAULT_HOURS["meal"]]
        return False, [DEFAULT_HOURS["attraction"]]

    return False, windows


def is_within_any_window(
    arrival_min: int, depart_min: int, windows: List[Tuple[int, int]]
) -> bool:
    """Check if [arrival_min, depart_min] fits inside at least one (start,end) window."""
    for start, end in windows:
        if arrival_min >= start and depart_min <= end:
            return True
    return False


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

        stats["total_stops"] += len([s for s in stops if s["role"] != "hotel"])

        for stop_idx, stop in enumerate(stops):
            poi_id_base = stop["poi_id"].rsplit("_day", 1)[0]
            poi = poi_lookup.get(poi_id_base)

            arrival_min = time_to_minutes(stop["arrival"])
            depart_min = time_to_minutes(stop["depart"])

            # Skip hotel for most checks
            if stop["role"] == "hotel":
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
                            "poi": stop["name"],
                            "overrun_minutes": overrun,
                        }
                    )
                    stats["day_overruns"].append(overrun)
                continue

            # 1. Consecutive meals
            if prev_stop and prev_stop["role"] == "meal" and stop["role"] == "meal":
                violations.append(
                    {
                        "type": "consecutive_meals",
                        "severity": "error",
                        "message": (
                            f"Consecutive meals ({prev_stop['name']} → {stop['name']})"
                        ),
                        "day": day_num,
                        "weekday": weekday_short,
                        "poi": stop["name"],
                    }
                )

            # 2. Meal timing
            if stop["role"] == "meal":
                meals_today += 1
                meal_type = get_meal_type(arrival_min)
                if meal_type == "other":
                    violations.append(
                        {
                            "type": "meal_timing",
                            "severity": "warning",
                            "message": (
                                f"Meal at unusual time ({stop['arrival']}) - "
                                f"{stop['name']}"
                            ),
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop["name"],
                            "arrival": stop["arrival"],
                        }
                    )

            # 3. Opening hours
            if poi and stop["role"] != "hotel" and date_obj is not None:
                role = stop["role"]
                closed_flag, windows = get_open_windows_for_date(
                    poi=poi, date_obj=date_obj, role=role
                )

                if closed_flag and not windows:
                    violations.append(
                        {
                            "type": "poi_closed",
                            "severity": "error",
                            "message": (
                                f"POI closed on {weekday_short} - {stop['name']}"
                            ),
                            "day": day_num,
                            "weekday": weekday_short,
                            "poi": stop["name"],
                        }
                    )
                    prev_stop = stop
                    continue

                # If 24h (0,1440), no check
                if windows != [(0, 24 * 60)]:
                    if not is_within_any_window(arrival_min, depart_min, windows):
                        # Use first window as representative for message
                        if windows:
                            s0, e0 = windows[0]
                            expected_str = (
                                f"{s0 // 60:02d}:{s0 % 60:02d}-"
                                f"{e0 // 60:02d}:{e0 % 60:02d}"
                            )
                        else:
                            expected_str = "unknown"

                        violations.append(
                            {
                                "type": "outside_hours",
                                "severity": "warning",
                                "message": (
                                    f"Visit outside hours "
                                    f"({stop['arrival']}-{stop['depart']}) - "
                                    f"{stop['name']}"
                                ),
                                "day": day_num,
                                "weekday": weekday_short,
                                "poi": stop["name"],
                                "expected_hours": expected_str,
                            }
                        )

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
    for day_idx, meal_count in enumerate(stats["meals_per_day"]):
        day_num = day_idx + 1
        # we don't know weekday here, so omit
        if meal_count < 1:
            violations.append(
                {
                    "type": "insufficient_meals",
                    "severity": "error",
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

    # 6. Theme balance against selected themes
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
                    "message": (
                        "Missing themes in itinerary: " + ", ".join(missing_themes)
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

        print(f"\n⚠️  Found {len(errors)} errors, {len(warnings)} warnings")

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

    # print("=" * 70 + "\n")


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
