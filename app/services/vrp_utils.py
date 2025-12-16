from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import List, Dict, Tuple, Optional, Union, Any

from app.services.vrp_model import DaySpec, Node, vrp_config, HotelEvent, HotelEventType


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


def adjust_window_for_hotel_events(
    window: Tuple[int, int],
    day_spec: DaySpec,
    service_time: int,
) -> Optional[Tuple[int, int]]:
    """
    Adjust a mandatory POI time window to avoid conflicts with hotel events.

    When a mandatory POI's requested window overlaps with hotel check-in/check-out,
    shift the POI window to before or after the hotel event. Mandatory POIs take
    priority, but hotel events have fixed time windows that cannot be moved.

    Strategy:
    - If window overlaps with check-in (14:00-16:00), try scheduling before or after
    - If window overlaps with check-out (10:00-12:00), try scheduling after
    - Choose the option that preserves more of the original window

    Args:
        window: Original (start_min, end_min) window for the POI
        day_spec: DaySpec containing hotel events for this day
        service_time: Service time needed at the POI

    Returns:
        Adjusted window tuple, or None if no valid window exists
    """
    if not day_spec.has_hotel_event:
        return window

    poi_start, poi_end = window
    day_start = day_spec.start_min
    day_end = day_spec.end_min

    # Collect all hotel event windows for this day
    hotel_windows = []
    for event in day_spec.hotel_events:
        hotel_windows.append((event.window[0], event.window[1], event.event_type))

    # Sort by start time
    hotel_windows.sort(key=lambda x: x[0])

    # Check for overlaps and find available slots
    def windows_overlap(w1: Tuple[int, int], w2: Tuple[int, int]) -> bool:
        return w1[0] < w2[1] and w2[0] < w1[1]

    # Check if POI window overlaps with any hotel event
    has_conflict = False
    for h_start, h_end, _ in hotel_windows:
        if windows_overlap((poi_start, poi_end), (h_start, h_end)):
            has_conflict = True
            break

    if not has_conflict:
        return window

    # Find available time slots around hotel events
    available_slots: List[Tuple[int, int]] = []

    # Slot before first hotel event
    if hotel_windows:
        first_hotel_start = hotel_windows[0][0]
        if day_start < first_hotel_start:
            available_slots.append((day_start, first_hotel_start))

    # Slots between hotel events
    for i in range(len(hotel_windows) - 1):
        gap_start = hotel_windows[i][1]
        gap_end = hotel_windows[i + 1][0]
        if gap_start < gap_end:
            available_slots.append((gap_start, gap_end))

    # Slot after last hotel event
    if hotel_windows:
        last_hotel_end = hotel_windows[-1][1]
        if last_hotel_end < day_end:
            available_slots.append((last_hotel_end, day_end))

    # Find the best slot that can accommodate the POI
    best_slot = None
    best_overlap = 0  # Prefer slot with most overlap with original window

    for slot_start, slot_end in available_slots:
        slot_duration = slot_end - slot_start
        if slot_duration < service_time:
            continue  # Slot too small

        # Calculate overlap with original window
        overlap_start = max(slot_start, poi_start)
        overlap_end = min(slot_end, poi_end)
        overlap = max(0, overlap_end - overlap_start)

        if overlap > best_overlap:
            best_overlap = overlap
            # Constrain POI window to fit within slot
            new_start = max(slot_start, poi_start)
            new_end = min(slot_end, poi_end)
            # Ensure minimum service time
            if new_end - new_start < service_time:
                new_end = min(new_start + service_time, slot_end)
            if new_end - new_start >= service_time:
                best_slot = (new_start, new_end)

    # If no overlapping slot found, use the largest available slot
    if best_slot is None:
        for slot_start, slot_end in available_slots:
            slot_duration = slot_end - slot_start
            if slot_duration >= service_time:
                best_slot = (slot_start, min(slot_start + (poi_end - poi_start), slot_end))
                break

    return best_slot


def determine_hotel_events(
    num_days: int,
    hotel: Dict[str, Any],
    is_first_city: bool = True,
    is_last_city: bool = True,
    prev_city_hotel: Optional[Dict[str, Any]] = None,
) -> Dict[int, List[HotelEvent]]:
    """
    Determine which days require hotel events based on trip structure.

    RULES:
    1. Single-day single-city trip: NO hotel events (no overnight stay)
    2. Each hotel has exactly ONE check-in and ONE check-out (paired events)
    3. Check-in always happens on FIRST day of a city segment
    4. Check-out always happens on FIRST day of the NEXT city segment (transition day)
       OR last day if this is the last city
    5. Single-day LAST destination: Only check-out from PREVIOUS hotel (no check-in)
    6. num_checkin == num_checkout (globally)
    7. hotel[i].checkin always before hotel[i].checkout in time

    TRANSITION DAY HANDLING:
    - When moving to a new city, check-out from previous hotel happens on day 0 of new city
    - This is handled by the NEW city segment, not the previous one
    - Previous city does NOT add checkout on its last day (unless it's the last city)

    Args:
        num_days: Number of days in this city segment
        hotel: Current city's hotel {id, name, lat, lon}
        is_first_city: Whether this is the first city in the trip
        is_last_city: Whether this is the last city in the trip
        prev_city_hotel: Previous city's hotel for transition day checkout

    Returns:
        Dict mapping day_index (0-based within segment) -> list of HotelEvent
    """
    events: Dict[int, List[HotelEvent]] = {}

    if num_days == 0:
        return events

    last_day_idx = num_days - 1

    # Rule 1: Single-day single-city trip - no hotel events
    if num_days == 1 and is_first_city and is_last_city:
        return events

    # Handle transition day: check-out from previous hotel on day 0 of this city
    if not is_first_city and prev_city_hotel:
        if 0 not in events:
            events[0] = []
        events[0].append(
            HotelEvent(
                event_type=HotelEventType.CHECK_OUT,
                hotel_id=str(prev_city_hotel["id"]),
                hotel_name=str(prev_city_hotel["name"]),
                lat=float(prev_city_hotel["lat"]),
                lon=float(prev_city_hotel["lon"]),
                window=vrp_config.hotel_check_out_window,
                service_time=vrp_config.hotel_service_time,
            )
        )

    # Rule 5: Single-day LAST destination - only checkout from prev (already added above)
    # No check-in to current hotel needed
    if num_days == 1 and is_last_city and not is_first_city:
        return events

    # From here: city needs hotel events for CURRENT hotel
    # Check-in on day 0
    if 0 not in events:
        events[0] = []
    events[0].append(
        HotelEvent(
            event_type=HotelEventType.CHECK_IN,
            hotel_id=str(hotel["id"]),
            hotel_name=str(hotel["name"]),
            lat=float(hotel["lat"]),
            lon=float(hotel["lon"]),
            window=vrp_config.hotel_check_in_window,
            service_time=vrp_config.hotel_service_time,
        )
    )

    # Check-out from current hotel:
    # - If this is the LAST city: checkout on last day of this segment
    # - If NOT last city: checkout will be handled by NEXT city's transition day
    if is_last_city:
        if last_day_idx not in events:
            events[last_day_idx] = []
        events[last_day_idx].append(
            HotelEvent(
                event_type=HotelEventType.CHECK_OUT,
                hotel_id=str(hotel["id"]),
                hotel_name=str(hotel["name"]),
                lat=float(hotel["lat"]),
                lon=float(hotel["lon"]),
                window=vrp_config.hotel_check_out_window,
                service_time=vrp_config.hotel_service_time,
            )
        )

    return events


def create_day_specs(
    maut_output: dict,
    hotel: Dict[str, Any],
    pacing: str,
    is_first_city: bool = True,
    is_last_city: bool = True,
    prev_city_hotel: Optional[Dict[str, Any]] = None,
) -> List[DaySpec]:
    """
    Create DaySpec objects with proper hotel event handling.

    Hotel events are only added when there's a real-world reason:
    - Check-in on first day of first city
    - Check-out on last day of last city
    - Both on city transition days

    Most days are "free days" with no hotel constraint.
    """
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

    # Determine hotel events for each day
    hotel_events = determine_hotel_events(
        num_days=num_days,
        hotel=hotel,
        is_first_city=is_first_city,
        is_last_city=is_last_city,
        prev_city_hotel=prev_city_hotel,
    )

    day_specs = []
    start_min = vrp_config.pace_day_start_min.get(pacing, 9 * 60)
    budget_min = vrp_config.pace_day_budget_min.get(pacing, 11 * 60)
    end_min = start_min + budget_min

    for k in range(num_days):
        day_hotel_events = hotel_events.get(k, [])

        day_specs.append(
            DaySpec(
                day_index=k,
                date=start_date + dt.timedelta(days=k),
                start_min=start_min,
                end_min=end_min,
                depot_id=str(hotel["id"]),
                hotel_events=day_hotel_events,
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

                # Check for conflicts with hotel events and adjust if needed
                original_window = (start, end)
                adjusted_window = adjust_window_for_hotel_events(
                    window=original_window,
                    day_spec=d,
                    service_time=end - start,
                )

                if adjusted_window is None:
                    # Cannot fit mandatory POI on this day due to hotel conflicts
                    return None

                wbd[day_specific] = [adjusted_window]
                # For specific time windows, use the full window duration as service time
                service = adjusted_window[1] - adjusted_window[0]
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


def create_hotel_event_nodes(
    day_specs: List[DaySpec],
    start_idx: int,
) -> Tuple[List[Node], int]:
    """
    Create mandatory hotel event nodes from DaySpec hotel events.

    Hotel events (check-in/check-out) are modeled as mandatory POI nodes with
    specific time windows. This ensures the solver respects hotel timing constraints.

    Args:
        day_specs: List of DaySpec with hotel_events
        start_idx: Starting node index

    Returns:
        (hotel_nodes, next_idx) tuple
    """
    nodes: List[Node] = []
    idx = start_idx

    for day in day_specs:
        for event in day.hotel_events:
            # Create a unique ID for this hotel event
            event_suffix = "checkin" if event.event_type == HotelEventType.CHECK_IN else "checkout"
            poi_id = f"{event.hotel_id}_{event_suffix}_day{day.day_index}"

            # Time window for this specific day only
            windows_by_day = {day.day_index: [event.window]}

            # Keep hotel name clean, store event type in dedicated field
            nodes.append(
                Node(
                    idx=idx,
                    poi_id=poi_id,
                    name=event.hotel_name,  # Clean name without suffix
                    role="accommodation",
                    lat=event.lat,
                    lon=event.lon,
                    service=event.service_time,
                    themes=None,
                    windows_by_day=windows_by_day,
                    is_mandatory=True,
                    maut_score=0.0,
                    hotel_event_type=event_suffix,  # "checkin" or "checkout"
                )
            )
            idx += 1

    return nodes, idx


def create_nodes(
    maut_output: dict,
    day_specs: List[DaySpec],
    hotel: Dict[str, Any],
    pacing: str,
    mandatory: Optional[Dict[str, Dict]] = None,
) -> List[Node]:
    """
    Create all nodes for the VRP problem.

    Node structure:
    - Index 0: Depot node (hotel, for backward compatibility)
    - Hotel event nodes: Mandatory check-in/check-out nodes with time windows
    - POI nodes: Attractions and meals

    The depot node is kept for backward compatibility but the solver should
    use hotel event nodes for actual scheduling constraints.
    """
    nodes: List[Node] = []
    idx = 0

    # Depot node (index 0) - kept for backward compatibility
    # On days without hotel events, the solver starts from time, not location
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

    # Create mandatory hotel event nodes (check-in/check-out)
    hotel_event_nodes, idx = create_hotel_event_nodes(day_specs, idx)
    nodes.extend(hotel_event_nodes)

    # POI nodes - include all POIs from MAUT (already theme-filtered by MAUT scoring)
    places = maut_output.get("places", [])
    for poi in places:
        roles = poi.get("roles", [])
        role = "attraction"  # Default role
        if "meal" in roles:
            role = "meal"
        elif "accommodation" in roles:
            # Skip accommodations as they are handled separately
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
    hotel: Dict[str, Any],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
    is_first_city: bool = True,
    is_last_city: bool = True,
    prev_city_hotel: Optional[Dict[str, Any]] = None,
) -> Tuple[List[DaySpec], List[Node], List[List[int]]]:
    """
    Convert MAUT output to the VRP problem format (DaySpecs, Nodes, Travel Matrix).

    This is the shared entry point for both OR-Tools CVRPTW and ACS-CVRPTW solvers.

    Hotel events are modeled correctly:
    - Check-in on first day of first city (14:00-16:00)
    - Check-out on last day of last city (10:00-12:00)
    - Both on city transition days
    - No hotel events on middle days (free days)

    Args:
        maut_output: MAUT output with places and meta
        hotel: Hotel dict with id, name, lat, lon
        pacing: "relaxed" | "balanced" | "packed"
        mandatory: Optional mandatory POI constraints {poi_id: {day, window, time_type}}
        is_first_city: Whether this is the first city in a multi-city trip
        is_last_city: Whether this is the last city in a multi-city trip
        prev_city_hotel: Previous city's hotel for transition day handling

    Returns:
        (day_specs, nodes, travel_matrix) tuple for solver input
    """
    # Import here to avoid circular dependency
    from app.services.osrm import osrm_client

    day_specs = create_day_specs(
        maut_output,
        hotel,
        pacing,
        is_first_city=is_first_city,
        is_last_city=is_last_city,
        prev_city_hotel=prev_city_hotel,
    )
    nodes = create_nodes(maut_output, day_specs, hotel, pacing, mandatory)

    # Create the travel matrix using OSRM
    coords = [(n.lat, n.lon) for n in nodes]
    travel_matrix = osrm_client.matrix_minutes(coords)

    return day_specs, nodes, travel_matrix


def build_multi_city_problem(
    city_segments: List[Dict[str, Any]],
    pacing: str = "balanced",
    mandatory: Optional[Dict[str, Dict]] = None,
) -> Tuple[List[DaySpec], List[Node], List[List[int]]]:
    """
    Build a unified VRP problem for multi-city trips with inter-city travel.

    This function creates a global OSRM matrix that includes:
    - All POIs across all cities
    - All hotel nodes (for check-in/check-out events)
    - Inter-city travel times

    Args:
        city_segments: List of city segment dicts, each containing:
            - maut_output: City's MAUT output
            - hotel: City's hotel
            - is_first_city: bool
            - is_last_city: bool
            - prev_city_hotel: Previous city's hotel (for transitions)
        pacing: Trip pacing
        mandatory: Mandatory POI constraints

    Returns:
        (day_specs, nodes, travel_matrix) tuple for solver input
    """
    from app.services.osrm import osrm_client

    all_day_specs: List[DaySpec] = []
    all_nodes: List[Node] = []
    global_day_offset = 0
    node_idx = 0

    # First pass: collect all nodes and day specs
    for segment in city_segments:
        maut_output = segment["maut_output"]
        hotel = segment["hotel"]
        is_first = segment.get("is_first_city", False)
        is_last = segment.get("is_last_city", False)
        prev_hotel = segment.get("prev_city_hotel")

        # Create day specs for this segment
        day_specs = create_day_specs(
            maut_output,
            hotel,
            pacing,
            is_first_city=is_first,
            is_last_city=is_last,
            prev_city_hotel=prev_hotel,
        )

        # Adjust day indices to be global
        for ds in day_specs:
            ds.day_index += global_day_offset

        all_day_specs.extend(day_specs)

        # Create nodes for this segment
        nodes = create_nodes(maut_output, day_specs, hotel, pacing, mandatory)

        # Adjust node indices to be global
        for node in nodes:
            node.idx = node_idx
            # Adjust windows_by_day keys to global day indices
            new_windows = {}
            for local_day, windows in node.windows_by_day.items():
                global_day = local_day + global_day_offset
                new_windows[global_day] = windows
            node.windows_by_day = new_windows
            node_idx += 1

        all_nodes.extend(nodes)
        global_day_offset += len(day_specs)

    # Create global travel matrix including inter-city travel
    coords = [(n.lat, n.lon) for n in all_nodes]
    travel_matrix = osrm_client.matrix_minutes(coords)

    return all_day_specs, all_nodes, travel_matrix
