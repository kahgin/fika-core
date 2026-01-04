"""Tests for pacing implementation - day windows and service times."""

import pytest
from unittest.mock import patch
from app.services.vrp_utils import create_day_specs, build_problem
from app.services.vrp_model import vrp_config


@pytest.fixture
def mock_osrm():
    with patch("app.services.osrm.osrm_client") as mock:

        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def hotel():
    return {"id": "hotel1", "name": "Test Hotel", "lat": 1.3, "lon": 103.8}


@pytest.fixture
def maut_output():
    return {
        "places": [
            {
                "id": "a1",
                "name": "Attraction",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["cultural"],
            },
            {"id": "m1", "name": "Meal", "roles": ["meal"], "coordinates": {"lat": 1.30, "lng": 103.84}},
        ],
        "meta": {"num_days": 2, "dates": {"type": "flexible", "days": 2}},
    }


class TestPacingDaySpecs:
    """Test day windows by pacing."""

    def test_relaxed_pacing(self, maut_output, hotel):
        day_specs = create_day_specs(maut_output, hotel, pacing="relaxed")
        expected_start = vrp_config.pace_day_start_min["relaxed"]
        expected_budget = vrp_config.pace_day_budget_min["relaxed"]
        for ds in day_specs:
            assert ds.start_min == expected_start
            assert ds.end_min - ds.start_min == expected_budget

    def test_packed_pacing(self, maut_output, hotel):
        day_specs = create_day_specs(maut_output, hotel, pacing="packed")
        expected_start = vrp_config.pace_day_start_min["packed"]
        expected_budget = vrp_config.pace_day_budget_min["packed"]
        for ds in day_specs:
            assert ds.start_min == expected_start
            assert ds.end_min - ds.start_min == expected_budget

    def test_packed_longer_than_relaxed(self, maut_output, hotel):
        ds_relaxed = create_day_specs(maut_output, hotel, pacing="relaxed")
        ds_packed = create_day_specs(maut_output, hotel, pacing="packed")
        relaxed_budget = ds_relaxed[0].end_min - ds_relaxed[0].start_min
        packed_budget = ds_packed[0].end_min - ds_packed[0].start_min
        assert packed_budget >= relaxed_budget


class TestPacingServiceTimes:
    """Test service times by pacing."""

    def test_attraction_service_times(self, mock_osrm, maut_output, hotel):
        for pacing in ["relaxed", "balanced", "packed"]:
            expected = vrp_config.service_time_min["attraction"][pacing]
            _, nodes, _ = build_problem(maut_output, hotel, pacing=pacing)
            attraction_nodes = [n for n in nodes if n.role == "attraction"]
            for node in attraction_nodes:
                assert node.service == expected

    def test_meal_service_times(self, mock_osrm, maut_output, hotel):
        for pacing in ["relaxed", "balanced", "packed"]:
            expected = vrp_config.service_time_min["meal"][pacing]
            _, nodes, _ = build_problem(maut_output, hotel, pacing=pacing)
            meal_nodes = [n for n in nodes if n.role == "meal"]
            for node in meal_nodes:
                assert node.service == expected
