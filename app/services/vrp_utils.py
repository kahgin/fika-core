from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import List, Dict, Tuple, Optional, Union

from app.services.vrp_model import vrp_config


# Constants for weekday names
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_time_minutes(t: int) -> str:
    """Convert minutes from midnight to a formatted 'HH:MM' string."""
    h = t // 60
    m = t % 60
    return f"{h:02d}:{m:02d}"


def parse_time_range_label(label: str) -> Optional[Tuple[int, int]]:
    """
    Parse a time range label to a tuple of minutes (start, end).
    
    Handles formats:
    - '10 am-9 pm' -> (600, 1260)
    - '11:45 am-2:30 pm' -> (705, 870)
    - 'Open 24 hours' -> (0, 1440)
    - 'Closed' -> None
    - Overnight ranges like '8 pm-2 am' -> (1200, 1440) (clamped to midnight)
    """
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
        # Handle overnight ranges like 8pm-2am by clamping to midnight
        if end_min <= start_min:
            end_min = 24 * 60

        return (start_min, end_min)
    except (ValueError, IndexError):
        return None


def normalize_open_hours_value(value: Union[str, List[str], None]) -> List[str]:
    """
    Normalize open_hours value to a list of interval strings.
    
    Handles:
    - None -> []
    - "10 am-9 pm" -> ["10 am-9 pm"]
    - ["10 am-2 pm", "5 pm-9 pm"] -> ["10 am-2 pm", "5 pm-9 pm"]
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def parse_weekday_intervals(
    open_hours: Optional[Dict[str, Union[str, List[str]]]],
    weekday: str,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """
    Parse all intervals for a specific weekday from open_hours dict.
    
    Returns:
        (is_closed, intervals) where:
        - is_closed: True if explicitly closed on this day
        - intervals: List of (start_min, end_min) tuples, empty if closed
    """
    if not open_hours or weekday not in open_hours:
        return (False, [])  # No data, not explicitly closed
    
    raw = open_hours.get(weekday)
    labels = normalize_open_hours_value(raw)
    
    if not labels:
        return (False, [])
    
    intervals: List[Tuple[int, int]] = []
    is_closed = False
    
    for label in labels:
        s = label.lower()
        if "closed" in s:
            is_closed = True
            continue
        parsed = parse_time_range_label(label)
        if parsed:
            intervals.append(parsed)
    
    # If explicitly closed and no valid intervals, return closed
    if is_closed and not intervals:
        return (True, [])
    
    return (False, intervals)


def get_all_open_intervals(
    open_hours: Optional[Dict[str, Union[str, List[str]]]],
) -> Dict[str, List[Tuple[int, int]]]:
    """
    Parse all weekday intervals from open_hours dict.
    
    Returns:
        Dict mapping weekday name -> list of (start_min, end_min) intervals.
        Days marked as closed will have empty list.
        Days with no data will not be in the dict.
    """
    if not open_hours:
        return {}
    
    result: Dict[str, List[Tuple[int, int]]] = {}
    
    for weekday in WEEKDAYS:
        is_closed, intervals = parse_weekday_intervals(open_hours, weekday)
        if is_closed:
            result[weekday] = []  # Explicitly closed
        elif intervals:
            result[weekday] = intervals
        # If no data for this day, don't include in result
    
    return result


def compute_representative_interval(
    open_hours: Optional[Dict[str, Union[str, List[str]]]],
    default_window: Tuple[int, int],
) -> Tuple[int, int]:
    """
    Compute the most common interval across all weekdays for unknown-day itineraries.
    
    Algorithm:
    1. Collect all numeric intervals across weekdays that are not closed
    2. Find the most common interval(s)
    3. If multiple equally common, choose earliest by start time
    4. If no intervals found, return default_window
    
    Returns:
        (start_min, end_min) tuple representing the best interval for scheduling
    """
    all_intervals = get_all_open_intervals(open_hours)
    
    if not all_intervals:
        return default_window
    
    # Collect all intervals from non-closed days
    interval_list: List[Tuple[int, int]] = []
    for weekday, intervals in all_intervals.items():
        if intervals:  # Skip closed days (empty list)
            interval_list.extend(intervals)
    
    if not interval_list:
        return default_window
    
    # Count occurrences of each interval
    counter = Counter(interval_list)
    max_count = max(counter.values())
    
    # Get all intervals with max count
    most_common = [interval for interval, count in counter.items() if count == max_count]
    
    # Choose earliest by start time
    most_common.sort(key=lambda x: (x[0], x[1]))
    
    return most_common[0]


def is_poi_open_on_date(
    open_hours: Optional[Dict[str, Union[str, List[str]]]],
    date: dt.date,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """
    Check if a POI is open on a specific date and return its intervals.
    
    Returns:
        (is_open, intervals) where:
        - is_open: False if explicitly closed, True otherwise
        - intervals: List of (start_min, end_min) tuples for that day
    """
    if not open_hours:
        return (True, [])  # No data means assume open
    
    weekday = weekday_name(date)
    is_closed, intervals = parse_weekday_intervals(open_hours, weekday)
    
    if is_closed:
        return (False, [])
    
    return (True, intervals)


def get_effective_windows(
    open_hours: Optional[Dict[str, Union[str, List[str]]]],
    date: Optional[dt.date],
    default_window: Tuple[int, int],
    use_representative: bool = False,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """
    Get effective time windows for scheduling a POI.
    
    Args:
        open_hours: POI's open_hours dict
        date: Specific date (None for unknown-day itinerary)
        default_window: Fallback window if no data
        use_representative: If True and date is None, compute representative interval
    
    Returns:
        (is_open, windows) where:
        - is_open: False if POI is closed on this date/day
        - windows: List of (start_min, end_min) tuples
    """
    if date is not None:
        # Date-specific itinerary
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        if not is_open:
            return (False, [])
        if intervals:
            return (True, intervals)
        return (True, [default_window])
    
    if use_representative:
        # Unknown-day itinerary: use representative interval
        interval = compute_representative_interval(open_hours, default_window)
        return (True, [interval])
    
    # Fallback to default
    return (True, [default_window])


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
