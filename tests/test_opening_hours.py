"""
Comprehensive tests for POI opening hours handling in the scheduling pipeline.

Tests cover:
1. Parsing open_hours for each weekday
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
        result = parse_time_range_label("10 am-9 pm")
        assert result == (10 * 60, 21 * 60)

    def test_with_minutes(self):
        result = parse_time_range_label("11:45 am-2:30 pm")
        assert result == (11 * 60 + 45, 14 * 60 + 30)

    def test_noon(self):
        result = parse_time_range_label("12 pm-1 pm")
        assert result == (12 * 60, 13 * 60)

    def test_midnight(self):
        result = parse_time_range_label("12 am-6 am")
        assert result == (0, 6 * 60)

    def test_open_24_hours(self):
        result = parse_time_range_label("Open 24 hours")
        assert result == (0, 24 * 60)

    def test_closed(self):
        result = parse_time_range_label("Closed")
        assert result is None

    def test_overnight_clamped(self):
        result = parse_time_range_label("8 pm-2 am")
        assert result == (20 * 60, 24 * 60)

    def test_invalid_format(self):
        result = parse_time_range_label("invalid")
        assert result is None


class TestNormalizeOpenHoursValue:
    """Tests for normalize_open_hours_value function."""

    def test_none_returns_empty_list(self):
        result = normalize_open_hours_value(None)
        assert result == []

    def test_string_returns_list(self):
        result = normalize_open_hours_value("10 am-9 pm")
        assert result == ["10 am-9 pm"]

    def test_list_returns_list(self):
        result = normalize_open_hours_value(["10 am-2 pm", "5 pm-9 pm"])
        assert result == ["10 am-2 pm", "5 pm-9 pm"]


class TestParseWeekdayIntervals:
    """Tests for parse_weekday_intervals function."""

    def test_single_interval(self):
        open_hours = {"Monday": ["10 am-9 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 21 * 60)]

    def test_multiple_intervals(self):
        open_hours = {"Monday": ["10 am-2 pm", "5 pm-9 pm"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 14 * 60), (17 * 60, 21 * 60)]

    def test_closed_day(self):
        open_hours = {"Monday": ["Closed"]}
        is_closed, intervals = parse_weekday_intervals(open_hours, "Monday")
        assert is_closed
        assert intervals == []


class TestComputeRepresentativeInterval:
    """Tests for compute_representative_interval function."""

    def test_most_common_interval(self):
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

    def test_all_closed_returns_default(self):
        open_hours = {day: ["Closed"] for day in WEEKDAYS}
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == default


class TestIsPoiOpenOnDate:
    """Tests for is_poi_open_on_date function."""

    def test_open_on_weekday(self):
        open_hours = {"Monday": ["10 am-9 pm"], "Sunday": ["Closed"]}
        date = dt.date(2024, 1, 15)
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        assert is_open
        assert intervals == [(10 * 60, 21 * 60)]

    def test_closed_on_weekday(self):
        open_hours = {"Monday": ["10 am-9 pm"], "Sunday": ["Closed"]}
        date = dt.date(2024, 1, 14)
        is_open, intervals = is_poi_open_on_date(open_hours, date)
        assert not is_open
        assert intervals == []


class TestValidatePoiScheduleAgainstHours:
    """Tests for validate_poi_schedule_against_hours function."""

    def test_valid_schedule(self):
        poi = {"open_hours": {"Monday": ["10 am-9 pm"]}}
        date = dt.date(2024, 1, 15)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert is_valid
        assert error == ""

    def test_poi_closed_on_date(self):
        poi = {"open_hours": {"Sunday": ["Closed"]}}
        date = dt.date(2024, 1, 14)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert not is_valid
        assert "closed" in error.lower()

    def test_visit_outside_hours(self):
        poi = {"open_hours": {"Monday": ["10 am-5 pm"]}}
        date = dt.date(2024, 1, 15)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 18 * 60, 20 * 60, date, "attraction"
        )
        assert not is_valid
        assert "outside hours" in error.lower()

    def test_24_hour_always_valid(self):
        poi = {"open_hours": {"Monday": ["Open 24 hours"]}}
        date = dt.date(2024, 1, 15)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 3 * 60, 5 * 60, date, "attraction"
        )
        assert is_valid


class TestValidateItinerary:
    """Integration tests for validate_itinerary function."""

    def _make_stop(self, poi_id, name, role, arrival, depart):
        return {
            "poi_id": poi_id,
            "name": name,
            "role": role,
            "arrival": arrival,
            "depart": depart,
            "start_service": arrival,
        }

    def test_valid_itinerary_with_open_hours(self):
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "open_hours": {"Monday": ["10:00 am-6:00 pm"]},
                    "themes": ["culture"],
                },
                {
                    "id": "poi2",
                    "name": "Restaurant",
                    "open_hours": {"Monday": ["11:00 am-10:00 pm"]},
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
                        self._make_stop(
                            "poi1", "Museum", "attraction", "10:30", "12:00"
                        ),
                        self._make_stop("poi2", "Restaurant", "meal", "12:30", "13:30"),
                        self._make_stop("hotel", "Hotel", "hotel", "14:00", "14:00"),
                    ],
                }
            ]
        }
        result = validate_itinerary(cvrptw_output, maut_output)
        errors = [v for v in result["violations"] if v["severity"] == "error"]
        poi_closed_errors = [v for v in errors if v["type"] == "poi_closed"]
        assert len(poi_closed_errors) == 0

    def test_poi_closed_on_scheduled_day(self):
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "open_hours": {"Sunday": ["Closed"]},
                    "themes": ["culture"],
                }
            ],
            "meta": {},
        }
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-14",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop(
                            "poi1", "Museum", "attraction", "10:00", "12:00"
                        ),
                        self._make_stop("hotel", "Hotel", "hotel", "13:00", "13:00"),
                    ],
                }
            ]
        }
        result = validate_itinerary(cvrptw_output, maut_output)
        errors = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(errors) == 1

    def test_24_hour_poi(self):
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "24h Store",
                    "open_hours": {"Monday": ["Open 24 hours"]},
                    "themes": ["shopping"],
                }
            ],
            "meta": {},
        }
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        self._make_stop("hotel", "Hotel", "hotel", "09:00", "09:00"),
                        self._make_stop(
                            "poi1", "24h Store", "attraction", "03:00", "04:00"
                        ),
                        self._make_stop("hotel", "Hotel", "hotel", "05:00", "05:00"),
                    ],
                }
            ]
        }
        result = validate_itinerary(cvrptw_output, maut_output)
        outside_hours = [
            v for v in result["violations"] if v["type"] == "outside_hours"
        ]
        assert len(outside_hours) == 0


class TestSchedulingIntegration:
    """Integration tests for scheduling with opening hours."""

    def test_poi_skipped_on_closed_day_in_cvrptw(self):
        from app.services.vrp_utils import build_problem

        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "coordinates": {"lat": 3.1, "lng": 101.6},
                    "roles": ["attraction"],
                    "open_hours": {
                        "Monday": ["Closed"],
                        "Tuesday": ["10:00 am-6:00 pm"],
                    },
                    "themes": ["culture"],
                },
                {
                    "id": "poi2",
                    "name": "Restaurant",
                    "coordinates": {"lat": 3.11, "lng": 101.61},
                    "roles": ["meal"],
                    "open_hours": {
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
        hotel = {"id": "hotel1", "name": "Test Hotel", "lat": 3.1, "lon": 101.6}

        day_specs, nodes, travel = build_problem(maut_output, hotel, pacing="balanced")

        museum_nodes = [n for n in nodes if "poi1" in n.poi_id]
        day0_museum = [n for n in museum_nodes if 0 in n.windows_by_day]
        for node in day0_museum:
            assert node.windows_by_day.get(0, []) == []


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_open_hours_dict(self):
        poi = {"open_hours": {}}
        date = dt.date(2024, 1, 15)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 11 * 60, 13 * 60, date, "attraction"
        )
        assert is_valid

    def test_nature_poi_default_24h(self):
        poi = {"themes": ["nature"]}
        date = dt.date(2024, 1, 15)
        is_valid, error = validate_poi_schedule_against_hours(
            poi, 5 * 60, 6 * 60, date, "attraction"
        )
        assert is_valid

    def test_poi_id_with_day_suffix(self):
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "open_hours": {"Monday": ["10:00 am-6:00 pm"]},
                    "themes": ["culture"],
                }
            ],
            "meta": {},
        }
        cvrptw_output = {
            "days": [
                {
                    "date": "2024-01-15",
                    "stops": [
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "09:00",
                            "depart": "09:00",
                        },
                        {
                            "poi_id": "poi1_day0",
                            "name": "Museum",
                            "role": "attraction",
                            "arrival": "11:00",
                            "depart": "13:00",
                        },
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "14:00",
                            "depart": "14:00",
                        },
                    ],
                }
            ]
        }
        result = validate_itinerary(cvrptw_output, maut_output)
        poi_closed = [v for v in result["violations"] if v["type"] == "poi_closed"]
        assert len(poi_closed) == 0
