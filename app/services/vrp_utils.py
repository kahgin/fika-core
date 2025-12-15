from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import List, Dict, Tuple, Optional, Union

from app.services.vrp_model import DaySpec, Node, vrp_config


# Constants for weekday names
WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# Time Formatting & Parsing


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


# Open Hours Parsing


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


# VRP Problem Building (shared by cvrptw.py and acs_cvrptw.py)


def create_day_specs(maut_output: dict, hotel: Dict[str, float], pacing: str) -> List[DaySpec]:
    """Create a list of DaySpec objects based on the trip duration and pacing."""
    meta = maut_output.get("meta", {})
    dates = meta.get("dates", {})
    num_days = meta.get("num_days", 1)

    start_date = dt.date.today()
    if dates.get("type") == "specific":
        start_raw = dates.get("start_date")
        if start_raw:
            try:
                start_date = dt.date.fromisoformat(str(start_raw).split("T")[0])
            except (ValueError, TypeError):
                pass

    day_specs = []
    start_min = vrp_config.pace_day_start_min.get(pacing, 9 * 60)
    budget_min = vrp_config.pace_day_budget_min.get(pacing, 11 * 60)
    end_min = start_min + budget_min

    for k in range(num_days):
        day_specs.append(
            DaySpec(
                day_index=k,
                date=start_date + dt.timedelta(days=k),
                start_min=start_min,
                end_min=end_min,
                depot_id=str(hotel["id"]),
            )
        )
    return day_specs


def create_poi_node(
    poi: Dict,
    role: str,
    idx: int,
    day_specs: List[DaySpec],
    pacing: str,
    mandatory: Optional[Dict[str, Dict]],
) -> Optional[Node]:
    """Create a single Node object for a POI, handling its schedule and constraints.

    Handles mandatory POI time_type modes:
    - 'specific': Use provided start_time/end_time window
    - 'all_day': Block entire day (day start to end)
    - 'any_time': Use role-based default windows (fallback)
    """
    coords = poi.get("coordinates")
    if not coords or coords.get("lat") is None or coords.get("lng") is None:
        return None

    service_times = vrp_config.service_time_min.get(role, {})
    service = service_times.get(pacing, 90)

    wbd: Dict[int, List[Tuple[int, int]]] = {}
    # Internal data uses snake_case
    open_hours = poi.get("open_hours")
    day_specific = poi.get("_day_specific")
    role_default = vrp_config.default_role_windows.get(role, (9 * 60, 21 * 60))

    # Check if this POI is mandatory and get its constraints
    base_id = str(poi["id"]).rsplit("_day", 1)[0]
    is_mand = False
    md_spec: Dict = {}

    if mandatory and base_id in mandatory:
        is_mand = True
        md_spec = mandatory[base_id] or {}

    # Get mandatory constraints
    day_constraint = md_spec.get("day")
    time_type = md_spec.get("time_type", "any_time")
    is_all_day = md_spec.get("all_day", False) or time_type == "all_day"
    window_constraint = md_spec.get("window")

    # If mandatory with day constraint, only create node for that specific day
    if is_mand and day_constraint is not None:
        target_day = int(day_constraint) - 1  # API uses 1-based indexing
        if day_specific != target_day:
            return None  # This node copy is for the wrong day

    if day_specific is not None:
        d = day_specs[day_specific]

        if is_mand and is_all_day:
            # All-day: block entire day window, use full day budget
            wbd[day_specific] = [(d.start_min, d.end_min)]
            # Set service time to fill the day (minus buffer for travel)
            service = max(service, d.end_min - d.start_min - 60)
        elif is_mand and window_constraint:
            # Specific time window from user
            try:
                start_parts = window_constraint[0].split(":")
                end_parts = window_constraint[1].split(":")
                start = (
                    int(start_parts[0]) * 60 + int(start_parts[1]) if len(start_parts) > 1 else int(start_parts[0]) * 60
                )
                end = int(end_parts[0]) * 60 + int(end_parts[1]) if len(end_parts) > 1 else int(end_parts[0]) * 60
                wbd[day_specific] = [(start, end)]
                # For specific time windows, use the full window duration as service time
                # User wants to be there from 09:00-16:00, so service = 7 hours
                service = end - start
            except (ValueError, IndexError):
                # Invalid format, fall back to role defaults
                day_default = (
                    max(d.start_min, role_default[0]),
                    min(d.end_min, role_default[1]),
                )
                windows = extract_windows_for_date(open_hours, d.date, day_default)
                if role == "meal":
                    windows = restrict_meal_windows(windows)
                if windows:
                    wbd[day_specific] = windows
        else:
            # any_time or no constraint: use role-based defaults
            day_default = (
                max(d.start_min, role_default[0]),
                min(d.end_min, role_default[1]),
            )
            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if role == "meal":
                windows = restrict_meal_windows(windows)
            if windows:
                wbd[day_specific] = windows

        if not wbd:
            return None  # Not visitable on its specific day
    else:
        # This branch is less likely if create_nodes creates day-specific copies
        for d in day_specs:
            day_default = (
                max(d.start_min, role_default[0]),
                min(d.end_min, role_default[1]),
            )
            windows = extract_windows_for_date(open_hours, d.date, day_default)
            if role == "meal":
                windows = restrict_meal_windows(windows)
            if windows:
                wbd[d.day_index] = windows
        if not wbd:
            return None  # Not visitable on any day

    return Node(
        idx=idx,
        poi_id=str(poi["id"]),
        name=str(poi.get("name")),
        role=role,
        themes=poi.get("themes", []),
        lat=float(coords["lat"]),
        lon=float(coords["lng"]),
        service=service,
        windows_by_day=wbd,
        is_mandatory=is_mand,
        maut_score=float(poi.get("_score", 0.0)),
    )


def create_nodes(
    maut_output: dict,
    day_specs: List[DaySpec],
    hotel: Dict[str, float],
    pacing: str,
    mandatory: Optional[Dict[str, Dict]] = None,
) -> List[Node]:
    """Create a list of all nodes (depot and POIs) for the VRP.

    Note: All attractions from MAUT are included (no theme filtering here).
    Theme diversity is enforced during route optimization via penalties and constraints.
    MAUT already pre-filters POIs by theme relevance during scoring.
    Excluded themes are filtered at the database level via p_excluded_themes.
    """
    nodes: List[Node] = []
    idx = 0

    # Depot node (index 0)
    nodes.append(
        Node(
            idx=idx,
            poi_id=str(hotel["id"]),
            name=str(hotel["name"]),
            role="depot",
            lat=float(hotel["lat"]),
            lon=float(hotel["lon"]),
            service=0,
            themes=None,
            windows_by_day={d.day_index: [(d.start_min, d.end_min)] for d in day_specs},
        )
    )
    idx += 1

    # POI nodes - include all POIs from MAUT (already theme-filtered by MAUT scoring)
    places = maut_output.get("places", [])
    for poi in places:
        roles = poi.get("roles", [])
        role = "attraction"  # Default role
        if "meal" in roles:
            role = "meal"
        elif "accommodation" in roles:
            # Skip accommodations as they are handled as depots
            continue

        # Each POI can be visited on any day, so create a version for each day
        for day_idx in range(len(day_specs)):
            poi_copy = poi.copy()
            poi_copy["id"] = f"{poi['id']}_day{day_idx}"
            poi_copy["_day_specific"] = day_idx
            new_node = create_poi_node(
                poi=poi_copy,
                role=role,
                idx=idx,
                day_specs=day_specs,
                pacing=pacing,
                mandatory=mandatory,
            )
            if new_node:
                nodes.append(new_node)
                idx += 1

    return nodes


def build_problem(
    maut_output: dict,
    hotel: Dict[str, float],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
) -> Tuple[List[DaySpec], List[Node], List[List[int]]]:
    """
    Convert MAUT output to the VRP problem format (DaySpecs, Nodes, Travel Matrix).

    This is the shared entry point for both OR-Tools CVRPTW and ACS-CVRPTW solvers.

    Args:
        maut_output: MAUT output with places and meta
        hotel: Hotel dict with id, name, lat, lon
        pacing: "relaxed" | "balanced" | "packed"
        mandatory: Optional mandatory POI constraints {poi_id: {day, window, time_type}}

    Returns:
        (day_specs, nodes, travel_matrix) tuple for solver input
    """
    # Import here to avoid circular dependency
    from app.services.osrm import osrm_client

    day_specs = create_day_specs(maut_output, hotel, pacing)
    nodes = create_nodes(maut_output, day_specs, hotel, pacing, mandatory)

    # Create the travel matrix using OSRM
    coords = [(n.lat, n.lon) for n in nodes]
    travel_matrix = osrm_client.matrix_minutes(coords)

    return day_specs, nodes, travel_matrix
