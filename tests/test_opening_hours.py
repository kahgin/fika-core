"""
Comprehensive tests for POI opening hours handling in the scheduling pipeline.

Tests cover:
1. Parsing openHours for each weekday
2. Unknown-day itinerary (days-only) using representative interval
3. Date-specific itinerary respecting actual weekday intervals
4. Scheduling constraints (single/multiple intervals, closed days, 24h)
"""

import datetime as dt
import pytest
from typing import Dict, Any, List, Tuple, Optional

from app.services.vrp_utils import (
    parse_time_range_label,
    normalize_open_hours_value,
    parse_weekday_intervals,
    get_all_open_intervals,
    compute_representative_interval,
    is_poi_open_on_date,
    get_effective_windows,
    extract_windows_for_date,
    WEEKDAYS,
)
from app.utils.validators import (
    get_open_windows_for_date,
    is_within_any_window,
    validate_poi_schedule_against_hours,
    validate_itinerary,
)


class TestParseTimeRangeLabel:
    """Tests for parse_time_range_label function."""

    def test_simple_am_pm(self):
        """Parse '10 am-9 pm' format."""
        result = parse_time_range_label("10 am-9 pm")
        assert result == (10 * 60, 21 * 60)

    def test_with_minutes(self):
        """Parse '11:45 am-2:30 pm' format."""
        result = parse_time_range_label("11:45 am-2:30 pm")
        assert result == (11 * 60 + 45, 14 * 60 + 30)

    def test_noon(self):
        """Parse times around noon correctly."""
        result = parse_time_range_label("12 pm-1 pm")
        assert result == (12 * 60, 13 * 60)

    def test_midnight(self):
        """Parse times around midnight correctly."""
        result = parse_time_range_label("12 am-6 am")
        assert result == (0, 6 * 60)

    def test_open_24_hours(self):
        """Parse 'Open 24 hours' to (0, 1440)."""
        result = parse_time_range_label("Open 24 hours")
        assert result == (0, 24 * 60)

    def test_open_24_hours_case_insensitive(self):
        """Parse 'open 24 hours' case-insensitively."""
        result = parse_time_range_label("OPEN 24 HOURS")
        assert result == (0, 24 * 60)

    def test_closed(self):
        """Parse 'Closed' returns None."""
        result = parse_time_range_label("Closed")
        assert result is None

    def test_closed_case_insensitive(self):
        """Parse 'closed' case-insensitively."""
        result = parse_time_range_label("CLOSED")
        assert result is None

    def test_overnight_clamped(self):
        """Overnight ranges like '8 pm-2 am' clamp to midnight."""
        result = parse_time_range_label("8 pm-2 am")
        assert result == (20 * 60, 24 * 60)

    def test_invalid_format(self):
        """Invalid format returns None."""
        result = parse_time_range_label("invalid")
        assert result is None

    def test_empty_string(self):
        """Empty string returns None."""
        result = parse_time_range_label("")
        assert result is None

    def test_whitespace_handling(self):
        """Handles extra whitespace."""
        result = parse_time_range_label("  10 am  -  9 pm  ")
        assert result == (10 * 60, 21 * 60)


class TestNormalizeOpenHoursValue:
    """Tests for normalize_open_hours_value function."""

    def test_none_returns_empty_list(self):
        """None returns empty list."""
        result = normalize_open_hours_value(None)
        assert result == []

    def test_string_returns_list(self):
        """String returns single-item list."""
        result = normalize_open_hours_value("10 am-9 pm")
        assert result == ["10 am-9 pm"]

    def test_list_returns_list(self):
        """List returns same list."""
        result = normalize_open_hours_value(["10 am-2 pm", "5 pm-9 pm"])
        assert result == ["10 am-2 pm", "5 pm-9 pm"]

    def test_empty_list(self):
        """Empty list returns empty list."""
        result = normalize_open_hours_value([])
        assert result == []


class TestParseWeekdayIntervals:
    """Tests for parse_weekday_intervals function."""

    def test_single_interval(self):
        """Parse single interval for a weekday."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 21 * 60)]

    def test_multiple_intervals(self):
        """Parse multiple intervals for a weekday."""
        open_hours = {"Monday": ["10 am-2 pm", "5 pm-9 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 14 * 60), (17 * 60, 21 * 60)]

    def test_closed_day(self):
        """Parse closed day."""
        open_hours = {"Monday": ["Closed"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert is_closed
        assert intervals == []

    def test_open_24_hours(self):
        """Parse 24-hour day."""
        open_hours = {"Monday": ["Open 24 hours"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(0, 24 * 60)]

    def test_missing_weekday(self):
        """Missing weekday returns no data."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Tuesday")
        assert not is_closed
        assert intervals == []

    def test_none_open_hours(self):
        """None open_hours returns no data."""
        is_closed, intervals = parse_weekday_intervals(None, "Monday")
        assert not is_closed
        assert intervals == []

    def test_string_value_normalized(self):
        """String value is normalized to list."""
        open_hours = {"Monday": "10 am-9 pm"}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 21 * 60)]


class TestGetAllOpenIntervals:
    """Tests for get_all_open_intervals function."""

    def test_full_week(self):
        """Parse full week of intervals."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Tuesday": ["10 am-9 pm"],
            "Wednesday": ["10 am-9 pm"],
            "Thursday": ["10 am-9 pm"],
            "Friday": ["10 am-10 pm"],
            "Saturday": ["9 am-10 pm"],
            "Sunday": ["Closed"],
        }
        result = get_all_open_intervals(open_hours)
        
        assert result["Monday"] == [(10 * 60, 21 * 60)]
        assert result["Friday"] == [(10 * 60, 22 * 60)]
        assert result["Saturday"] == [(9 * 60, 22 * 60)]
        assert result["Sunday"] == []

    def test_empty_open_hours(self):
        """Empty open_hours returns empty dict."""
        result = get_all_open_intervals({})
        assert result == {}

    def test_none_open_hours(self):
        """None open_hours returns empty dict."""
        result = get_all_open_intervals(None)
        assert result == {}


class TestComputeRepresentativeInterval:
    """Tests for compute_representative_interval function."""

    def test_most_common_interval(self):
        """Select most common interval across weekdays."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Tuesday": ["10 am-9 pm"],
            "Wednesday": ["10 am-9 pm"],
            "Thursday": ["10 am-9 pm"],
            "Friday": ["10 am-10 pm"],
            "Saturday": ["9 am-10 pm"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == (10 * 60, 21 * 60)

    def test_tie_breaker_earliest_start(self):
        """When tied, choose earliest by start time."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Tuesday": ["10 am-9 pm"],
            "Wednesday": ["9 am-8 pm"],
            "Thursday": ["9 am-8 pm"],
            "Friday": ["Closed"],
            "Saturday": ["Closed"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == (9 * 60, 20 * 60)

    def test_all_closed_returns_default(self):
        """All closed days returns default window."""
        open_hours = {
            "Monday": ["Closed"],
            "Tuesday": ["Closed"],
            "Wednesday": ["Closed"],
            "Thursday": ["Closed"],
            "Friday": ["Closed"],
            "Saturday": ["Closed"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == default

    def test_no_open_hours_returns_default(self):
        """No open_hours returns default window."""
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(None, default)
        assert result == default

    def test_multiple_intervals_per_day(self):
        """Handle multiple intervals per day."""
        open_hours = {
            "Monday": ["10 am-2 pm", "5 pm-9 pm"],
            "Tuesday": ["10 am-2 pm", "5 pm-9 pm"],
            "Wednesday": ["10 am-2 pm", "5 pm-9 pm"],
            "Thursday": ["10 am-9 pm"],
            "Friday": ["10 am-9 pm"],
            "Saturday": ["Closed"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == (10 * 60, 14 * 60)

    def test_24_hour_days(self):
        """Handle 24-hour days."""
        open_hours = {
            "Monday": ["Open 24 hours"],
            "Tuesday": ["Open 24 hours"],
            "Wednesday": ["Open 24 hours"],
            "Thursday": ["10 am-9 pm"],
            "Friday": ["10 am-9 pm"],
            "Saturday": ["Closed"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == (0, 24 * 60)


class TestIsPoiOpenOnDate:
    """Tests for is_poi_open_on_date function."""

    def test_open_on_weekday(self):
        """POI is open on a specific weekday."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Sunday": ["Closed"],
        }
        date = dt.date(2024, 1, 15)
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        assert is_open
        assert intervals == [(10 * 60, 21 * 60)]

    def test_closed_on_weekday(self):
        """POI is closed on a specific weekday."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Sunday": ["Closed"],
        }
        date = dt.date(2024, 1, 14)
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        assert not is_open
        assert intervals == []

    def test_no_data_for_weekday(self):
        """No data for weekday assumes open."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
        }
        date = dt.date(2024, 1, 16)
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        assert is_open
        assert intervals == []

    def test_no_open_hours_assumes_open(self):
        """No open_hours assumes open."""
        date = dt.date(2024, 1, 15)
        is_open, intervals = is_poi_open_on_date(None, date)
        assert is_open
        assert intervals == []


class TestGetEffectiveWindows:
    """Tests for get_effective_windows function."""

    def test_date_specific_open(self):
        """Date-specific: POI is open."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        date = dt.date(2024, 1, 15)
        default = (9 * 60, 21 * 60)
        
        is_open, windows = get_effective_windows(open_hours, date, default)
        assert is_open
        assert windows == [(10 * 60, 21 * 60)]

    def test_date_specific_closed(self):
        """Date-specific: POI is closed."""
        open_hours = {"Sunday": ["Closed"]}
        date = dt.date(2024, 1, 14)
        default = (9 * 60, 21 * 60)
        
        is_open, windows = get_effective_windows(open_hours, date, default)
        assert not is_open
        assert windows == []

    def test_date_specific_no_data_uses_default(self):
        """Date-specific: No data uses default."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        date = dt.date(2024, 1, 16)
        default = (9 * 60, 21 * 60)
        
        is_open, windows = get_effective_windows(open_hours, date, default)
        assert is_open
        assert windows == [default]

    def test_unknown_day_representative(self):
        """Unknown-day: Use representative interval."""
        open_hours = {
            "Monday": ["10 am-9 pm"],
            "Tuesday": ["10 am-9 pm"],
            "Wednesday": ["10 am-9 pm"],
            "Thursday": ["10 am-9 pm"],
            "Friday": ["10 am-10 pm"],
            "Saturday": ["Closed"],
            "Sunday": ["Closed"],
        }
        default = (9 * 60, 21 * 60)
        
        is_open, windows = get_effective_windows(
            open_hours, None, default, use_representative=True
        )
        assert is_open
        assert windows == [(10 * 60, 21 * 60)]

    def test_unknown_day_no_representative_uses_default(self):
        """Unknown-day without representative flag uses default."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        default = (9 * 60, 21 * 60)
        
        is_open, windows = get_effective_windows(
            open_hours, None, default, use_representative=False
        )
        assert is_open
        assert windows == [default]


class TestExtractWindowsForDate:
    """Tests for extract_windows_for_date function."""

    def test_basic_extraction(self):
        """Extract windows for a date."""
        open_hours = {"Monday": ["10 am-9 pm"]}
        date = dt.date(2024, 1, 15)
        default = (9 * 60, 21 * 60)
        
        windows = extract_windows_for_date(open_hours, date, default)
        assert windows == [(10 * 60, 21 * 60)]

    def test_closed_returns_empty(self):
        """Closed day returns empty list."""
        open_hours = {"Sunday": ["Closed"]}
        date = dt.date(2024, 1, 14)
        default = (9 * 60, 21 * 60)
        
        windows = extract_windows_for_date(open_hours, date, default)
        assert windows == []

    def test_no_data_returns_default(self):
        """No data returns default."""
        open_hours = {}
        date = dt.date(2024, 1, 15)
        default = (9 * 60, 21 * 60)
        
        windows = extract_windows_for_date(open_hours, date, default)
        assert windows == [default]

    def test_intersects_with_default(self):
        """Windows are intersected with default."""
        open_hours = {"Monday": ["8 am-10 pm"]}
        date = dt.date(2024, 1, 15)
        default = (9 * 60, 21 * 60)
        
        windows = extract_windows_for_date(open_hours, date, default)
        assert windows == [(9 * 60, 21 * 60)]


class TestIsWithinAnyWindow:
    """Tests for is_within_any_window function."""

    def test_within_single_window(self):
        """Visit within single window."""
        windows = [(10 * 60, 21 * 60)]
        assert is_within_any_window(11 * 60, 13 * 60, windows)

    def test_outside_single_window(self):
        """Visit outside single window."""
        windows = [(10 * 60, 21 * 60)]
        assert not is_within_any_window(8 * 60, 9 * 60, windows)

    def test_within_one_of_multiple_windows(self):
        """Visit within one of multiple windows."""
        windows = [(10 * 60, 14 * 60), (17 * 60, 21 * 60)]
        assert is_within_any_window(18 * 60, 20 * 60, windows)

    def test_spanning_gap_between_windows(self):
        """Visit spanning gap between windows fails."""
        windows = [(10 * 60, 14 * 60), (17 * 60, 21 * 60)]
        assert not is_within_any_window(13 * 60, 18 * 60, windows)

    def test_exact_window_boundaries(self):
        """Visit exactly at window boundaries."""
        windows = [(10 * 60, 21 * 60)]
        assert is_within_any_window(10 * 60, 21 * 60, windows)

    def test_empty_windows(self):
        """Empty windows always fails."""
        assert not is_within_any_window(10 * 60, 11 * 60, [])


class TestValidatePoiScheduleAgainstHours:
    """Tests for validate_poi_schedule_against_hours function."""

    def test_valid_schedule(self):
        """Valid schedule within hours."""
        poi = {"openHours": {"Monday": ["10 am-9 pm"]}}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert is_valid
        assert error == ""

    def test_poi_closed_on_date(self):
        """POI closed on date."""
        poi = {"openHours": {"Sunday": ["Closed"]}}
        date = dt.date(2024, 1, 14)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert not is_valid
        assert "closed" in error.lower()

    def test_visit_outside_hours(self):
        """Visit outside opening hours."""
        poi = {"openHours": {"Monday": ["10 am-5 pm"]}}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 18 * 60, 20 * 60, date, "attraction"
        )
        assert not is_valid
        assert "outside hours" in error.lower()

    def test_24_hour_always_valid(self):
        """24-hour POI always valid."""
        poi = {"openHours": {"Monday": ["Open 24 hours"]}}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 3 * 60, 5 * 60, date, "attraction"
        )
        assert is_valid

    def test_no_open_hours_uses_default(self):
        """No openHours uses default window."""
        poi = {}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert is_valid


class TestValidateItinerary:
    """Integration tests for validate_itinerary function."""

    def _make_stop(
        self,
        poi_id: str,
        name: str,
        role: str,
        arrival: str,
        depart: str,
    ) -> Dict[str, Any]:
        """Helper to create a stop dict."""
        return {
            "poi_id": poi_id,
            "name": name,
            "role": role,
            "arrival": arrival,
            "depart": depart,
            "start_service": arrival,
        }

    def test_valid_itinerary_with_open_hours(self):
        """Valid itinerary respecting opening hours."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "openHours": {"Monday": ["10:00 am-6:00 pm"]},
                    "themes": ["culture"],
                },
                {
                    "id": "poi2",
                    "name": "Restaurant",
                    "openHours": {"Monday": ["11:00 am-10:00 pm"]},
                    "themes": ["food"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "Museum", "attraction", "10:30", "12:00"),
                        self._make_stop("poi2", "Restaurant", "meal", "12:30", "13:30"),
                        self._make_stop("hotel", "Hotel", "hotel", "14:00", "14:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        errors = [v for v in result["violations"] if v["severity"] == "error"]
        poi_closed_errors = [v for v in errors if v["type"] == "poi_closed"]
        outside_hours_errors = [v for v in errors if v["type"] == "outside_hours"]
        assert len(poi_closed_errors) == 0
        assert len(outside_hours_errors) == 0

    def test_poi_closed_on_scheduled_day(self):
        """Detect POI scheduled on closed day."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "openHours": {"Sunday": ["Closed"]},
                    "themes": ["culture"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-14",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "Museum", "attraction", "10:00", "12:00"),
                        self._make_stop("hotel", "Hotel", "hotel", "13:00", "13:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        errors = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(errors) == 1
        assert "Museum" in errors[0]["message"]

    def test_visit_outside_opening_hours(self):
        """Detect visit outside opening hours."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "openHours": {"Monday": ["10:00 am-5:00 pm"]},
                    "themes": ["culture"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "Museum", "attraction", "18:00", "20:00"),
                        self._make_stop("hotel", "Hotel", "hotel", "21:00", "21:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        warnings = [v for v in result["violations"] if v["type"] == "outside_hours"]
        assert len(warnings) == 1
        assert "Museum" in warnings[0]["message"]

    def test_multiple_intervals_per_day(self):
        """Valid visit within one of multiple intervals."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Restaurant",
                    "openHours": {"Monday": ["11:00 am-2:00 pm", "6:00 pm-10:00 pm"]},
                    "themes": ["food"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "Restaurant", "meal", "19:00", "20:30"),
                        self._make_stop("hotel", "Hotel", "hotel", "21:00", "21:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        outside_hours = [v for v in result["violations"] if v["type"] == "outside_hours"]
        assert len(outside_hours) == 0

    def test_24_hour_poi(self):
        """24-hour POI accepts any time."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "24h Convenience Store",
                    "openHours": {"Monday": ["Open 24 hours"]},
                    "themes": ["shopping"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "24h Store", "attraction", "03:00", "04:00"),
                        self._make_stop("hotel", "Hotel", "hotel", "05:00", "05:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        outside_hours = [v for v in result["violations"] if v["type"] == "outside_hours"]
        poi_closed = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(outside_hours) == 0
        assert len(poi_closed) == 0

    def test_unknown_day_itinerary_no_date(self):
        """Unknown-day itinerary (no date) uses defaults."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "openHours": {"Monday": ["10:00 am-6:00 pm"]},
                    "themes": ["culture"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop("poi1", "Museum", "attraction", "11:00", "13:00"),
                        self._make_stop("hotel", "Hotel", "hotel", "14:00", "14:00"),
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        poi_closed = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(poi_closed) == 0


class TestSchedulingIntegration:
    """Integration tests for scheduling with opening hours."""

    def test_poi_skipped_on_closed_day_in_cvrptw(self):
        """Test that cvrptw.py correctly skips POIs on closed days."""
        from app.services.cvrptw import build_problem
        
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "coordinates": {"lat": 3.1, "lng": 101.6},
                    "poi_roles": ["attraction"],
                    "openHours": {
                        "Monday": ["Closed"],
                        "Tuesday": ["10:00 am-6:00 pm"],
                    },
                    "themes": ["culture"],
                },
                {
                    "id": "poi2",
                    "name": "Restaurant",
                    "coordinates": {"lat": 3.11, "lng": 101.61},
                    "poi_roles": ["meal"],
                    "openHours": {
                        "Monday": ["11:00 am-10:00 pm"],
                        "Tuesday": ["11:00 am-10:00 pm"],
                    },
                    "themes": ["food"],
                },
            ],
            "meta": {
                "num_days": 2,
                "dates": {
                    "type": "specific",
                    "start_date": "2024-01-15",
                    "end_date": "2024-01-16",
                },
            },
        }
        
        hotel = {
            "id": "hotel1",
            "name": "Test Hotel",
            "lat": 3.1,
            "lon": 101.6,
        }
        
        day_specs, nodes, travel = build_problem(maut_output, hotel, pacing="balanced")
        
        museum_nodes = [n for n in nodes if "poi1" in n.poi_id]
        
        day0_museum = [n for n in museum_nodes if 0 in n.windows_by_day]
        for node in day0_museum:
            assert node.windows_by_day.get(0, []) == []
        
        day1_museum = [n for n in museum_nodes if 1 in n.windows_by_day]
        assert len(day1_museum) > 0
        for node in day1_museum:
            assert len(node.windows_by_day.get(1, [])) > 0


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_open_hours_dict(self):
        """Empty openHours dict uses defaults."""
        poi = {"openHours": {}}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert is_valid

    def test_mixed_closed_and_open_intervals(self):
        """Handle mixed closed and open in same day."""
        open_hours = {"Monday": ["Closed", "10 am-5 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 17 * 60)]

    def test_overnight_hours_clamped(self):
        """Overnight hours are clamped to midnight."""
        result = parse_time_range_label("10 pm-2 am")
        assert result == (22 * 60, 24 * 60)

    def test_nature_poi_default_24h(self):
        """Nature POIs default to 24h."""
        poi = {"themes": ["nature"]}
        date = dt.date(2024, 1, 15)
        
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 5 * 60, 6 * 60, date, "attraction"
        )
        assert is_valid

    def test_poi_id_with_day_suffix(self):
        """Handle poi_id with _dayX suffix."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "openHours": {"Monday": ["10:00 am-6:00 pm"]},
                    "themes": ["culture"],
                },
            ],
            "meta": {},
        }
        
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        {"poi_id": "hotel", "name": "Hotel", "role": "hotel", 
                         "arrival": "09:00", "depart": "09:00"},
                        {"poi_id": "poi1_day0", "name": "Museum", "role": "attraction",
                         "arrival": "11:00", "depart": "13:00"},
                        {"poi_id": "hotel", "name": "Hotel", "role": "hotel",
                         "arrival": "14:00", "depart": "14:00"},
                    ],
                }
            ]
        }
        
        result = validate_itinerary(cvrptw_output, maut_output)
        poi_closed = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(poi_closed) == 0
