"""
Tests for meal scheduling constraints in the VRP solvers.

Constraints:
- Target 3 meals per day (soft constraint)
- Meals within preferred windows: breakfast (7-10), lunch (12-14), dinner (18-21)
- No consecutive meals
- Max 3 meals per day (hard cap)
"""

import pytest
from unittest.mock import patch

from app.services.vrp_model import vrp_config
from app.services.vrp_utils import restrict_meal_windows


class TestMealWindows:
    """Tests for meal window configuration."""

    def test_meal_windows_defined(self):
        """Test that meal windows are defined in config."""
        assert hasattr(vrp_config, "meal_windows")
        assert len(vrp_config.meal_windows) == 3  # breakfast, lunch, dinner

    def test_meal_windows_order(self):
        """Test meal windows are in chronological order."""
        windows = vrp_config.meal_windows
        for i in range(len(windows) - 1):
            assert windows[i][1] <= windows[i + 1][0], "Meal windows should not overlap"

    def test_breakfast_window(self):
        """Test breakfast window is morning."""
        breakfast = vrp_config.meal_windows[0]
        assert breakfast[0] >= 6 * 60  # After 6am
        assert breakfast[1] <= 11 * 60  # Before 11am

    def test_lunch_window(self):
        """Test lunch window is midday."""
        lunch = vrp_config.meal_windows[1]
        assert lunch[0] >= 11 * 60  # After 11am
        assert lunch[1] <= 15 * 60  # Before 3pm

    def test_dinner_window(self):
        """Test dinner window is evening."""
        dinner = vrp_config.meal_windows[2]
        assert dinner[0] >= 17 * 60  # After 5pm
        assert dinner[1] <= 22 * 60  # Before 10pm


class TestRestrictMealWindows:
    """Tests for restrict_meal_windows function."""

    def test_restrict_to_meal_times(self):
        """Test that windows are restricted to meal times."""
        # Full day window
        windows = [(8 * 60, 22 * 60)]
        restricted = restrict_meal_windows(windows)

        # Should have windows near meal times
        assert len(restricted) > 0

        # All restricted windows should overlap with meal windows
        for start, end in restricted:
            overlaps_meal = False
            for m_start, m_end in vrp_config.meal_windows:
                if start < m_end + vrp_config.meal_hard_tol_min and end > m_start - vrp_config.meal_hard_tol_min:
                    overlaps_meal = True
                    break
            assert overlaps_meal, f"Window ({start}, {end}) doesn't overlap any meal window"

    def test_empty_windows(self):
        """Test empty windows returns empty."""
        result = restrict_meal_windows([])
        assert result == []

    def test_window_outside_meal_times(self):
        """Test window completely outside meal times."""
        # 3am-5am - no meal windows
        windows = [(3 * 60, 5 * 60)]
        restricted = restrict_meal_windows(windows)
        assert restricted == []

    def test_window_overlaps_lunch(self):
        """Test window overlapping lunch time."""
        windows = [(11 * 60, 15 * 60)]
        restricted = restrict_meal_windows(windows)
        assert len(restricted) > 0

    def test_multiple_windows_merged(self):
        """Test overlapping restricted windows are merged."""
        # Window spanning multiple meal times
        windows = [(7 * 60, 21 * 60)]
        restricted = restrict_meal_windows(windows)

        # Should be merged into fewer windows
        for i in range(len(restricted) - 1):
            # No overlapping windows
            assert restricted[i][1] < restricted[i + 1][0]


class TestMealConstraintsPenalties:
    """Tests for meal-related penalties in config."""

    def test_consecutive_meal_penalty(self):
        """Test penalty for consecutive meals is defined."""
        assert hasattr(vrp_config, "penalty_meal_to_meal")
        assert vrp_config.penalty_meal_to_meal > 0

    def test_meal_shortfall_penalty(self):
        """Test penalty for missing meals is defined."""
        assert hasattr(vrp_config, "meal_shortfall_penalty")
        assert vrp_config.meal_shortfall_penalty > 0

    def test_meal_hard_tolerance(self):
        """Test meal time tolerance is defined."""
        assert hasattr(vrp_config, "meal_hard_tol_min")
        assert vrp_config.meal_hard_tol_min >= 0


class TestMealNodeCreation:
    """Tests for meal node creation in build_problem."""

    @pytest.fixture
    def mock_osrm(self):
        """Mock OSRM client."""
        with patch("app.services.osrm.osrm_client") as mock:

            def matrix_minutes(coords):
                n = len(coords)
                return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

            mock.matrix_minutes.side_effect = matrix_minutes
            yield mock

    def test_meal_nodes_have_restricted_windows(self, mock_osrm):
        """Test that meal nodes have windows restricted to meal times."""
        from app.services.vrp_utils import build_problem

        maut_output = {
            "places": [
                {
                    "id": "restaurant1",
                    "name": "Restaurant",
                    "roles": ["meal"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "open_hours": {"Monday": ["8:00 am-10:00 pm"]},
                },
            ],
            "meta": {
                "num_days": 1,
                "dates": {"type": "specific", "start_date": "2025-01-13"},  # Monday
            },
        }
        hotel = {"id": "hotel1", "name": "Hotel", "lat": 1.3, "lon": 103.8}

        day_specs, nodes, travel = build_problem(maut_output, hotel, pacing="balanced")

        # Find meal nodes
        meal_nodes = [n for n in nodes if n.role == "meal"]
        assert len(meal_nodes) > 0

        # Meal windows should be restricted
        for node in meal_nodes:
            for day_idx, windows in node.windows_by_day.items():
                for start, end in windows:
                    # Should be near a meal window
                    near_meal = False
                    for m_start, m_end in vrp_config.meal_windows:
                        if (
                            start < m_end + vrp_config.meal_hard_tol_min
                            and end > m_start - vrp_config.meal_hard_tol_min
                        ):
                            near_meal = True
                            break
                    assert near_meal, f"Meal window ({start}, {end}) not near any meal time"


class TestMealValidation:
    """Tests for meal validation in validators."""

    def test_meal_timing_validation(self):
        """Test that unusual meal times are flagged."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [
                {"id": "meal1", "name": "Restaurant", "themes": []},
            ],
            "meta": {},
        }

        # Meal at 3am - unusual time
        cvrptw_output = {
            "days": [
                {
                    "date": "2025-01-15",
                    "stops": [
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "02:00",
                            "depart": "02:00",
                        },
                        {
                            "poi_id": "meal1",
                            "name": "Restaurant",
                            "role": "meal",
                            "arrival": "03:00",
                            "depart": "04:00",
                        },
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "05:00",
                            "depart": "05:00",
                        },
                    ],
                }
            ]
        }

        result = validate_itinerary(cvrptw_output, maut_output)
        warnings = [v for v in result["violations"] if v["type"] == "meal_timing"]
        assert len(warnings) > 0

    def test_consecutive_meals_validation(self):
        """Test that consecutive meals are flagged as error."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [
                {"id": "meal1", "name": "Restaurant 1", "themes": []},
                {"id": "meal2", "name": "Restaurant 2", "themes": []},
            ],
            "meta": {},
        }

        cvrptw_output = {
            "days": [
                {
                    "date": "2025-01-15",
                    "stops": [
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "09:00",
                            "depart": "09:00",
                        },
                        {
                            "poi_id": "meal1",
                            "name": "Restaurant 1",
                            "role": "meal",
                            "arrival": "12:00",
                            "depart": "13:00",
                        },
                        {
                            "poi_id": "meal2",
                            "name": "Restaurant 2",
                            "role": "meal",
                            "arrival": "13:30",
                            "depart": "14:30",
                        },
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "15:00",
                            "depart": "15:00",
                        },
                    ],
                }
            ]
        }

        result = validate_itinerary(cvrptw_output, maut_output)
        errors = [v for v in result["violations"] if v["type"] == "consecutive_meals"]
        assert len(errors) > 0
