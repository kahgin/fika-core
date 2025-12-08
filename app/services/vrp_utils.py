from __future__ import annotations

import datetime as dt
from typing import List, Dict, Tuple, Optional

from app.services.vrp_model import vrp_config


def format_time_minutes(t: int) -> str:
    """Convert minutes from midnight to a formatted 'HH:MM' string."""
    h = t // 60
    m = t % 60
    return f"{h:02d}:{m:02d}"


def parse_time_range_label(label: str) -> Optional[Tuple[int, int]]:
    """Parse a time range label like '10 am-9 pm' to a tuple of minutes (600, 1260)."""
    s = label.strip().lower()
    if "closed" in s:
        return None
    if "open 24 hours" in s:
        return (0, 24 * 60)

    try:
        left, right = [x.strip() for x in s.split("-")]

        def to_min(x: str) -> int:
            x = x.replace(" ", "")
            ampm = "am" if "am" in x else "pm"
            hhmm = x.replace("am", "").replace("pm", "")
            if ":" in hhmm:
                h, m = map(int, hhmm.split(":"))
            else:
                h, m = int(hhmm), 0

            if ampm == "am" and h == 12:  # Midnight case
                h = 0
            elif ampm == "pm" and h != 12:
                h += 12

            return h * 60 + m

        start_min, end_min = to_min(left), to_min(right)
        # Handle overnight ranges like 8pm-2am by assuming end is next day
        if end_min <= start_min:
            end_min = 24 * 60

        return (start_min, end_min)
    except (ValueError, IndexError):
        return None


def weekday_name(d: dt.date) -> str:
    """Return the full weekday name (e.g., 'Monday')."""
    return d.strftime("%A")


def extract_windows_for_date(
    open_hours: Optional[Dict[str, List[str]]],
    date: dt.date,
    default_window: Tuple[int, int],
) -> List[Tuple[int, int]]:
    """Extract and parse time windows for a specific date from the open_hours dict."""
    if not open_hours:
        return [default_window]

    day_name = weekday_name(date)
    raw_labels = open_hours.get(day_name)
    if not raw_labels:
        return [default_window]

    windows = []
    is_closed = any("closed" in label.lower() for label in raw_labels)
    if is_closed:
        return []

    for label in raw_labels:
        parsed = parse_time_range_label(label)
        if parsed:
            # Intersect with the day's default operating window
            start = max(parsed[0], default_window[0])
            end = min(parsed[1], default_window[1])
            if start < end:
                windows.append((start, end))

    return windows if windows else [default_window]


def restrict_meal_windows(
    windows: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """
    Restrict meal POI windows to be near preferred meal times (breakfast, lunch, dinner).
    This is used to enforce that meals are taken at reasonable times.
    """
    if not windows:
        return []

    allowed = []
    for w_start, w_end in windows:
        for m_start, m_end in vrp_config.meal_windows:
            # Expand the meal window by the configured tolerance
            ms = m_start - vrp_config.meal_hard_tol_min
            me = m_end + vrp_config.meal_hard_tol_min
            # Find the intersection
            overlap_start = max(w_start, ms)
            overlap_end = min(w_end, me)
            if overlap_start < overlap_end:
                allowed.append((overlap_start, overlap_end))

    if not allowed:
        return []

    # Merge overlapping intervals to produce a clean list of windows
    allowed.sort()
    merged = []
    cur_start, cur_end = allowed[0]
    for next_start, next_end in allowed[1:]:
        if next_start <= cur_end:
            cur_end = max(cur_end, next_end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = next_start, next_end
    merged.append((cur_start, cur_end))

    return merged
