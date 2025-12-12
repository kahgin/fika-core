"""
Tests for theme diversity constraints in the VRP solvers.

Constraints:
- Max 2 attractions with same primary theme per day
- Penalty for consecutive same-theme attractions
- Theme distribution tracking
"""

import pytest
from unittest.mock import patch

from app.services.vrp_model import vrp_config
from app.services.pipeline import validate_global_rules


class TestThemeConfig:
    """Tests for theme-related configuration."""

    def test_max_theme_per_day_defined(self):
        """Test max theme per day is defined."""
        assert hasattr(vrp_config, "max_theme_per_day")
        assert vrp_config.max_theme_per_day >= 1

    def test_same_theme_penalty_defined(self):
        """Test penalty for same theme is defined."""
        assert hasattr(vrp_config, "penalty_same_theme")
        assert vrp_config.penalty_same_theme > 0


class TestThemeValidation:
    """Tests for theme validation in validate_global_rules."""

    def test_two_same_theme_passes(self):
        """Test 2 attractions with same theme passes."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["nature"]},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_three_same_theme_fails(self):
        """Test 3 attractions with same theme fails."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        assert not validation["ok"]
        assert any("theme" in e.lower() for e in validation["errors"])

    def test_primary_theme_only(self):
        """Test only primary (first) theme is counted."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture", "history"]},
                        {"role": "attraction", "themes": ["culture", "art"]},
                        {
                            "role": "attraction",
                            "themes": ["history", "culture"],
                        },  # history is primary
                    ]
                }
            ],
            "meta": {},
        }

        # Only 2 have "culture" as primary
        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_theme_per_day_independent(self):
        """Test theme limit is per-day."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                    ]
                },
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                    ]
                },
            ],
            "meta": {},
        }

        # 2 per day is within limit
        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_empty_themes_ignored(self):
        """Test attractions with empty themes don't trigger limit."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": []},
                        {"role": "attraction", "themes": []},
                        {"role": "attraction", "themes": []},
                        {"role": "attraction", "themes": []},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_meals_not_counted(self):
        """Test meals don't count toward theme limit."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal", "themes": ["food"]},
                        {"role": "meal", "themes": ["food"]},
                        {"role": "meal", "themes": ["food"]},
                        {"role": "attraction", "themes": ["culture"]},
                    ]
                }
            ],
            "meta": {},
        }

        # Meals shouldn't trigger theme limit
        validation = validate_global_rules(result)
        # Only check theme errors, not meal count
        theme_errors = [e for e in validation["errors"] if "theme" in e.lower()]
        assert len(theme_errors) == 0


class TestThemeDistribution:
    """Tests for theme distribution tracking in validators."""

    def test_theme_distribution_tracked(self):
        """Test theme distribution is tracked in stats."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [
                {"id": "poi1", "name": "Museum", "themes": ["culture"]},
                {"id": "poi2", "name": "Park", "themes": ["nature"]},
                {"id": "poi3", "name": "Temple", "themes": ["culture", "history"]},
            ],
            "meta": {"selected_themes": ["culture", "nature"]},
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
                            "poi_id": "poi1",
                            "name": "Museum",
                            "role": "attraction",
                            "arrival": "10:00",
                            "depart": "12:00",
                        },
                        {
                            "poi_id": "poi2",
                            "name": "Park",
                            "role": "attraction",
                            "arrival": "13:00",
                            "depart": "15:00",
                        },
                        {
                            "poi_id": "poi3",
                            "name": "Temple",
                            "role": "attraction",
                            "arrival": "16:00",
                            "depart": "18:00",
                        },
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "19:00",
                            "depart": "19:00",
                        },
                    ],
                }
            ]
        }

        result = validate_itinerary(cvrptw_output, maut_output)

        assert "theme_distribution" in result["stats"]
        assert result["stats"]["theme_distribution"].get("culture", 0) >= 1
        assert result["stats"]["theme_distribution"].get("nature", 0) >= 1

    def test_missing_themes_info(self):
        """Test missing themes are reported as info."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [
                {"id": "poi1", "name": "Museum", "themes": ["culture"]},
            ],
            "meta": {"selected_themes": ["culture", "nature", "shopping"]},
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
                            "poi_id": "poi1",
                            "name": "Museum",
                            "role": "attraction",
                            "arrival": "10:00",
                            "depart": "12:00",
                        },
                        {
                            "poi_id": "hotel",
                            "name": "Hotel",
                            "role": "hotel",
                            "arrival": "13:00",
                            "depart": "13:00",
                        },
                    ],
                }
            ]
        }

        result = validate_itinerary(cvrptw_output, maut_output)

        # Missing themes should be info level
        info_violations = [v for v in result["violations"] if v["severity"] == "info"]
        theme_info = [v for v in info_violations if v["type"] == "theme_imbalance"]
        assert len(theme_info) > 0
        assert "nature" in theme_info[0].get(
            "missing_themes", []
        ) or "shopping" in theme_info[0].get("missing_themes", [])


class TestThemeNodeCreation:
    """Tests for theme handling in node creation."""

    @pytest.fixture
    def mock_osrm(self):
        """Mock OSRM client."""
        with patch("app.services.osrm.osrm_client") as mock:

            def matrix_minutes(coords):
                n = len(coords)
                return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

            mock.matrix_minutes.side_effect = matrix_minutes
            yield mock

    def test_nodes_preserve_themes(self, mock_osrm):
        """Test that nodes preserve POI themes."""
        from app.services.vrp_utils import build_problem

        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Museum",
                    "roles": ["attraction"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "themes": ["culture", "history"],
                },
            ],
            "meta": {"num_days": 1},
        }
        hotel = {"id": "hotel1", "name": "Hotel", "lat": 1.3, "lon": 103.8}

        day_specs, nodes, travel = build_problem(maut_output, hotel, pacing="balanced")

        # Find attraction nodes
        attraction_nodes = [n for n in nodes if n.role == "attraction"]
        assert len(attraction_nodes) > 0

        for node in attraction_nodes:
            assert node.themes == ["culture", "history"]
