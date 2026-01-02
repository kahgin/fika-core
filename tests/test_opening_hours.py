"""Tests for POI opening hours handling."""

import datetime as dt
from app.services.vrp_utils import (
    parse_time_range_label,
    normalize_open_hours_value,
    parse_weekday_intervals,
    compute_representative_interval,
    is_poi_open_on_date,
    WEEKDAYS,
)


class TestParseTimeRangeLabel:
    """Tests for time range parsing."""

    def test_simple_am_pm(self):
        assert parse_time_range_label("10 am-9 pm") == (10 * 60, 21 * 60)

    def test_with_minutes(self):
        assert parse_time_range_label("11:45 am-2:30 pm") == (11 * 60 + 45, 14 * 60 + 30)

    def test_noon_midnight(self):
        assert parse_time_range_label("12 pm-1 pm") == (12 * 60, 13 * 60)
        assert parse_time_range_label("12 am-6 am") == (0, 6 * 60)

    def test_open_24_hours(self):
        assert parse_time_range_label("Open 24 hours") == (0, 24 * 60)

    def test_closed(self):
        assert parse_time_range_label("Closed") is None

    def test_invalid(self):
        assert parse_time_range_label("invalid") is None


class TestNormalizeOpenHours:
    """Tests for normalizing open hours values."""

    def test_none(self):
        assert normalize_open_hours_value(None) == []

    def test_string(self):
        assert normalize_open_hours_value("10 am-9 pm") == ["10 am-9 pm"]

    def test_list(self):
        assert normalize_open_hours_value(["10 am-2 pm", "5 pm-9 pm"]) == ["10 am-2 pm", "5 pm-9 pm"]


class TestParseWeekdayIntervals:
    """Tests for weekday interval parsing."""

    def test_single_interval(self):
        is_closed, intervals = parse_weekday_intervals({"Monday": ["10 am-9 pm"]}, "Monday")
        assert not is_closed
        assert intervals == [(10 * 60, 21 * 60)]

    def test_multiple_intervals(self):
        is_closed, intervals = parse_weekday_intervals({"Monday": ["10 am-2 pm", "5 pm-9 pm"]}, "Monday")
        assert not is_closed
        assert len(intervals) == 2

    def test_closed_day(self):
        is_closed, intervals = parse_weekday_intervals({"Monday": ["Closed"]}, "Monday")
        assert is_closed
        assert intervals == []


class TestIsPoiOpenOnDate:
    """Tests for POI availability on specific dates."""

    def test_open_on_weekday(self):
        open_hours = {"Monday": ["10 am-9 pm"], "Sunday": ["Closed"]}
        is_open, intervals = is_poi_open_on_date(open_hours, dt.date(2024, 1, 15))
        assert is_open
        assert intervals == [(10 * 60, 21 * 60)]

    def test_closed_on_weekday(self):
        open_hours = {"Monday": ["10 am-9 pm"], "Sunday": ["Closed"]}
        is_open, intervals = is_poi_open_on_date(open_hours, dt.date(2024, 1, 14))
        assert not is_open
        assert intervals == []


class TestComputeRepresentativeInterval:
    """Tests for representative interval computation."""

    def test_most_common(self):
        open_hours = {day: ["10 am-9 pm"] for day in WEEKDAYS[:5]}
        open_hours["Saturday"] = ["9 am-10 pm"]
        open_hours["Sunday"] = ["Closed"]
        default = (9 * 60, 21 * 60)
        result = compute_representative_interval(open_hours, default)
        assert result == (10 * 60, 21 * 60)

    def test_all_closed_returns_default(self):
        open_hours = {day: ["Closed"] for day in WEEKDAYS}
        default = (9 * 60, 21 * 60)
        assert compute_representative_interval(open_hours, default) == default
