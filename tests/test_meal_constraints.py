"""Tests for meal scheduling constraints."""

import pytest
from unittest.mock import patch
from app.services.vrp_model import vrp_config
from app.services.vrp_utils import restrict_meal_windows


class TestMealWindows:
    """Tests for meal window configuration."""

    def test_three_meal_windows(self):
        assert len(vrp_config.meal_windows) == 3

    def test_windows_chronological(self):
        windows = vrp_config.meal_windows
        for i in range(len(windows) - 1):
            assert windows[i][1] <= windows[i + 1][0]

    def test_breakfast_morning(self):
        breakfast = vrp_config.meal_windows[0]
        assert breakfast[0] >= 6 * 60
        assert breakfast[1] <= 11 * 60

    def test_lunch_midday(self):
        lunch = vrp_config.meal_windows[1]
        assert lunch[0] >= 11 * 60
        assert lunch[1] <= 15 * 60

    def test_dinner_evening(self):
        dinner = vrp_config.meal_windows[2]
        assert dinner[0] >= 17 * 60
        assert dinner[1] <= 22 * 60


class TestRestrictMealWindows:
    """Tests for restrict_meal_windows function."""

    def test_empty_input(self):
        assert restrict_meal_windows([]) == []

    def test_outside_meal_times(self):
        result = restrict_meal_windows([(3 * 60, 5 * 60)])  # 3-5am
        assert result == []

    def test_overlaps_lunch(self):
        result = restrict_meal_windows([(11 * 60, 15 * 60)])
        assert len(result) > 0


class TestMealNodeCreation:
    """Tests for meal node creation."""

    @pytest.fixture
    def mock_osrm(self):
        with patch("app.services.osrm.osrm_client") as mock:
            def matrix_minutes(coords):
                n = len(coords)
                return [[10 if i != j else 0 for j in range(n)] for i in range(n)]
            mock.matrix_minutes.side_effect = matrix_minutes
            yield mock

    def test_meal_nodes_restricted(self, mock_osrm):
        from app.services.vrp_utils import build_problem
        maut = {
            "places": [{"id": "r1", "name": "Restaurant", "roles": ["meal"],
                       "coordinates": {"lat": 1.3, "lng": 103.8},
                       "open_hours": {"Monday": ["8:00 am-10:00 pm"]}}],
            "meta": {"num_days": 1, "dates": {"type": "specific", "start_date": "2025-01-13"}},
        }
        hotel = {"id": "h1", "name": "Hotel", "lat": 1.3, "lon": 103.8}
        _, nodes, _ = build_problem(maut, hotel, pacing="balanced")
        
        meal_nodes = [n for n in nodes if n.role == "meal"]
        assert len(meal_nodes) > 0
        for node in meal_nodes:
            for windows in node.windows_by_day.values():
                for start, end in windows:
                    near_meal = any(
                        start < m_end + vrp_config.meal_hard_tol_min and
                        end > m_start - vrp_config.meal_hard_tol_min
                        for m_start, m_end in vrp_config.meal_windows
                    )
                    assert near_meal
