"""Date and weekday formatting utilities."""

from datetime import date, timedelta
from typing import Dict, Any, Optional


def compute_num_days(dates: Dict[str, Any]) -> Optional[int]:
    """
    Compute number of days from dates dict.

    Args:
        dates: Dict with type ('flexible' or 'specific') and relevant fields

    Returns:
        Number of days, or None if cannot be computed
    """
    if not isinstance(dates, dict):
        return None

    date_type = dates.get("type")

    if date_type == "flexible":
        try:
            d = int(dates.get("days") or 0)
            return max(1, min(30, d)) if d > 0 else None
        except (ValueError, TypeError):
            return None

    if date_type == "specific" and dates.get("start_date") and dates.get("end_date"):
        try:
            start = date.fromisoformat(str(dates["start_date"]).split("T")[0])
            end = date.fromisoformat(str(dates["end_date"]).split("T")[0])
            return max(1, (end - start).days + 1)
        except (ValueError, TypeError):
            return None

    return None


def time_to_minutes(time_str: str, default: int = 9 * 60) -> int:
    """
    Parse HH:MM time string to minutes since midnight.

    Args:
        time_str: Time string in HH:MM format (e.g., "09:30")
        default: Default value to return if parsing fails (default: 540 = 9:00 AM)

    Returns:
        Minutes since midnight (0-1439)
    """
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return default


def format_day_label(
    day_index: int,
    dates_info: Optional[Dict[str, Any]] = None,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Format day label based on date type (specific vs flexible).

    For specific dates: Returns day number, date, and weekday
    For flexible dates: Returns only day number (ignores any existing dates)

    Args:
        day_index: 0-based day index
        dates_info: Dates metadata with type, start_date, end_date
        date_str: Optional explicit date string (ISO format)

    Returns:
        Dict with:
        - day: int (1-based day number)
        - date: str | None (ISO format date for specific dates)
        - weekday: str | None (full weekday name for specific dates)
        - label: str (formatted label for display)
    """
    day_num = day_index + 1

    # Check dates_info for type first
    date_type = dates_info.get("type") if dates_info and isinstance(dates_info, dict) else None

    # For flexible dates, always return only day number (no dates)
    if date_type == "flexible":
        return {
            "day": day_num,
            "date": None,
            "weekday": None,
            "label": f"Day {day_num}",
        }

    # For specific dates, calculate from start_date or use explicit date
    if date_type == "specific":
        # Try to use start_date from dates_info
        start_date_str = dates_info.get("start_date") if dates_info else None
        if start_date_str:
            try:
                start_date = date.fromisoformat(str(start_date_str).split("T")[0])
                current_date = start_date + timedelta(days=day_index)
                weekday = current_date.strftime("%A")
                weekday_short = current_date.strftime("%a")
                return {
                    "day": day_num,
                    "date": current_date.isoformat(),
                    "weekday": weekday,
                    "label": f"{weekday_short}, {current_date.strftime('%b %d')}",
                }
            except (ValueError, AttributeError):
                pass

        # Fallback to explicit date_str if start_date not available
        if date_str:
            try:
                d = date.fromisoformat(str(date_str).split("T")[0])
                weekday = d.strftime("%A")
                weekday_short = d.strftime("%a")
                return {
                    "day": day_num,
                    "date": d.isoformat(),
                    "weekday": weekday,
                    "label": f"{weekday_short}, {d.strftime('%b %d')}",
                }
            except (ValueError, AttributeError):
                pass

    # No dates_info or fallback - just return day number
    return {
        "day": day_num,
        "date": None,
        "weekday": None,
        "label": f"Day {day_num}",
    }


def recompute_day_labels(days: list[Dict[str, Any]], dates_info: Optional[Dict[str, Any]] = None) -> None:
    """
    Recompute day labels for all days in-place.

    Updates each day dict with:
    - day: int (1-based)
    - date: str | None (None for flexible dates)
    - weekday: str | None (None for flexible dates)
    - label: str (for display)

    For flexible dates, this will clear any existing dates/weekdays.

    Args:
        days: List of day dicts to update
        dates_info: Dates metadata with type, start_date, end_date
    """
    date_type = dates_info.get("type") if dates_info and isinstance(dates_info, dict) else None

    for idx, day in enumerate(days):
        # For flexible dates, ignore existing date - always use "Day X" format
        # For specific dates, try to use existing date or calculate from start_date
        existing_date = day.get("date") if date_type == "specific" else None
        label_info = format_day_label(idx, dates_info, existing_date)

        day["day"] = label_info["day"]
        day["date"] = label_info["date"]
        day["weekday"] = label_info["weekday"]
        day["label"] = label_info["label"]
