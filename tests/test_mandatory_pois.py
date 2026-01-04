"""
Tests for mandatory POI handling.

4 cases:
1. Specific day/time - POI scheduled on specific day with time window
2. All day - POI blocks entire day (only that POI + hotel events)
3. Any time - POI on any day/time using role defaults
4. Fallback - No time_type defaults to any_time
"""

import pytest
from unittest.mock import patch
from app.services.vrp_utils import build_problem


@pytest.fixture
def mock_osrm():
    with patch("app.services.osrm.osrm_client") as mock:
        mock.matrix_minutes.side_effect = lambda coords: [
            [10 if i != j else 0 for j in range(len(coords))] for i in range(len(coords))
        ]
        yield mock


@pytest.fixture
def hotel():
    return {"id": "hotel1", "name": "Test Hotel", "lat": 1.3, "lon": 103.8}


@pytest.fixture
def basic_maut():
    return {
        "places": [
            {
                "id": "a1",
                "name": "Attraction",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["cultural_history"],
            },
            {"id": "m1", "name": "Meal", "roles": ["meal"], "coordinates": {"lat": 1.30, "lng": 103.84}},
            {
                "id": "mandatory_poi",
                "name": "Must Visit",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.40, "lng": 103.79},
                "themes": ["family"],
            },
        ],
        "meta": {"num_days": 3, "dates": {"type": "flexible", "days": 3}},
    }


class TestSpecificDayTime:
    """Case 1: specific day and time window."""

    def test_specific_day_and_time(self, mock_osrm, basic_maut, hotel):
        """POI with day=2 and window=[10:00, 12:00] appears on day 1 (0-indexed)."""
        mandatory = {"mandatory_poi": {"day": 2, "time_type": "specific", "window": ["10:00", "12:00"]}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert len(mand_nodes) == 1
        assert list(mand_nodes[0].windows_by_day.keys()) == [1]
        assert mand_nodes[0].windows_by_day[1] == [(600, 720)]


class TestAllDay:
    """Case 2: all-day POI blocks the day."""

    def test_all_day_has_extended_service(self, mock_osrm, basic_maut, hotel):
        """All-day POI has service time filling most of window."""
        mandatory = {"mandatory_poi": {"day": 2, "time_type": "all_day"}}
        day_specs, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        mand = mand_nodes[0]
        windows = mand.windows_by_day[1]
        window_duration = windows[0][1] - windows[0][0]
        assert mand.service >= window_duration - 60

    def test_all_day_sets_flag(self, mock_osrm, basic_maut, hotel):
        """All-day POI has is_all_day=True."""
        mandatory = {"mandatory_poi": {"day": 2, "time_type": "all_day"}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert mand_nodes[0].is_all_day is True

    def test_all_day_only_affects_specified_day(self, mock_osrm, basic_maut, hotel):
        """All-day POI with day=2 only creates a node for day 1 (0-indexed)."""
        mandatory = {"mandatory_poi": {"day": 2, "time_type": "all_day"}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        # Should only have 1 node (for day 2 which is index 1)
        assert len(mand_nodes) == 1
        assert list(mand_nodes[0].windows_by_day.keys()) == [1]
        assert mand_nodes[0].is_all_day is True


class TestAnyTime:
    """Case 3: any_time uses role defaults."""

    def test_any_time_creates_nodes_for_all_days(self, mock_osrm, basic_maut, hotel):
        """any_time without day creates nodes for all days."""
        mandatory = {"mandatory_poi": {"time_type": "any_time"}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert len(mand_nodes) == 3  # One for each day

    def test_any_time_with_day_constraint(self, mock_osrm, basic_maut, hotel):
        """any_time with day only appears on that day."""
        mandatory = {"mandatory_poi": {"day": 3, "time_type": "any_time"}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert len(mand_nodes) == 1
        assert list(mand_nodes[0].windows_by_day.keys()) == [2]


class TestFallback:
    """Case 4: Missing time_type defaults to any_time."""

    def test_no_time_type_defaults(self, mock_osrm, basic_maut, hotel):
        """Missing time_type defaults to any_time."""
        mandatory = {"mandatory_poi": {}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert len(mand_nodes) == 3  # any_time behavior


class TestEdgeCases:
    """Edge cases."""

    def test_day_out_of_range(self, mock_osrm, basic_maut, hotel):
        """Day beyond trip length creates no nodes."""
        mandatory = {"mandatory_poi": {"day": 10, "time_type": "specific", "window": ["10:00", "12:00"]}}
        _, nodes, _ = build_problem(basic_maut, hotel, pacing="balanced", mandatory=mandatory)

        mand_nodes = [n for n in nodes if n.is_mandatory and n.role != "accommodation"]
        assert len(mand_nodes) == 0
