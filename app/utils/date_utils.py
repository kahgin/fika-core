"""Date and weekday formatting utilities."""

from datetime import date, timedelta
from typing import Dict, Any, Optional


def format_day_label(
    day_index: int,
    dates_info: Optional[Dict[str, Any]] = None,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Format day label based on date type (specific vs flexible).

    For specific dates: Returns day number, date, and weekday
    For flexible dates: Returns only day number

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

    # If explicit date provided, use it
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

    # Check dates_info for type
    if not dates_info or not isinstance(dates_info, dict):
        return {
            "day": day_num,
            "date": None,
            "weekday": None,
            "label": f"Day {day_num}",
        }

    date_type = dates_info.get("type")

    if date_type == "specific":
        start_date_str = dates_info.get("start_date")
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

    # Flexible dates or fallback
    return {
        "day": day_num,
        "date": None,
        "weekday": None,
        "label": f"Day {day_num}",
    }


def recompute_day_labels(
    days: list[Dict[str, Any]], dates_info: Optional[Dict[str, Any]] = None
) -> None:
    """
    Recompute day labels for all days in-place.

    Updates each day dict with:
    - day: int (1-based)
    - date: str | None
    - weekday: str | None
    - label: str (for display)

    Args:
        days: List of day dicts to update
        dates_info: Dates metadata with type, start_date, end_date
    """
    for idx, day in enumerate(days):
        # Check if day already has explicit date
        existing_date = day.get("date")
        label_info = format_day_label(idx, dates_info, existing_date)

        day["day"] = label_info["day"]
        day["date"] = label_info["date"]
        day["weekday"] = label_info["weekday"]
        # Don't overwrite label if not needed, but ensure consistency
        if "label" not in day or day.get("label") != label_info["label"]:
            day["label"] = label_info["label"]
