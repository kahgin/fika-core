"""
Tests for theme diversity - config, distribution tracking, and node creation.

Note: Theme validation via validate_global_rules is tested in test_validation.py
"""

import pytest
from unittest.mock import patch
from app.services.vrp_model import vrp_config


class TestThemeConfig:
    """Tests for theme-related configuration in vrp_config."""

    def test_same_theme_penalty_defined(self):
        assert hasattr(vrp_config, "penalty_same_theme")
        assert vrp_config.penalty_same_theme > 0

    def test_theme_diversity_bonus_defined(self):
        assert hasattr(vrp_config, "theme_diversity_bonus")
        assert vrp_config.theme_diversity_bonus > 0


class TestThemeDistribution:
    """Tests for theme distribution tracking in validators."""

    def test_theme_distribution_tracked(self):
        """Theme distribution is tracked in validation stats."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [
                {"id": "poi1", "name": "Museum", "themes": ["cultural_history"]},
                {"id": "poi2", "name": "Park", "themes": ["nature"]},
            ],
            "meta": {"selected_themes": ["cultural_history", "nature"]},
        }
        cvrptw_output = {
            "days": [{
                "date": "2025-01-15",
                "stops": [
                    {"poi_id": "hotel", "name": "Hotel", "role": "hotel", "arrival": "09:00", "depart": "09:00"},
                    {"poi_id": "poi1", "name": "Museum", "role": "attraction", "arrival": "10:00", "depart": "12:00"},
                    {"poi_id": "poi2", "name": "Park", "role": "attraction", "arrival": "13:00", "depart": "15:00"},
                    {"poi_id": "hotel", "name": "Hotel", "role": "hotel", "arrival": "16:00", "depart": "16:00"},
                ],
            }]
        }

        result = validate_itinerary(cvrptw_output, maut_output)
        assert "theme_distribution" in result["stats"]
        assert result["stats"]["theme_distribution"].get("cultural_history", 0) >= 1

    def test_missing_themes_reported(self):
        """Missing themes are reported as info violations."""
        from app.utils.validators import validate_itinerary

        maut_output = {
            "places": [{"id": "poi1", "name": "Museum", "themes": ["cultural_history"]}],
            "meta": {"selected_themes": ["cultural_history", "nature", "shopping"]},
        }
        cvrptw_output = {
            "days": [{
                "date": "2025-01-15",
                "stops": [
                    {"poi_id": "hotel", "name": "Hotel", "role": "hotel", "arrival": "09:00", "depart": "09:00"},
                    {"poi_id": "poi1", "name": "Museum", "role": "attraction", "arrival": "10:00", "depart": "12:00"},
                    {"poi_id": "hotel", "name": "Hotel", "role": "hotel", "arrival": "13:00", "depart": "13:00"},
                ],
            }]
        }

        result = validate_itinerary(cvrptw_output, maut_output)
        info_violations = [v for v in result["violations"] if v["severity"] == "info"]
        theme_info = [v for v in info_violations if v["type"] == "theme_imbalance"]
        assert len(theme_info) > 0


class TestThemeNodeCreation:
    """Tests for theme handling in node creation."""

    @pytest.fixture
    def mock_osrm(self):
        with patch("app.services.osrm.osrm_client") as mock:
            mock.matrix_minutes.side_effect = lambda coords: [[10 if i != j else 0 for j in range(len(coords))] for i in range(len(coords))]
            yield mock

    def test_nodes_preserve_themes(self, mock_osrm):
        """Nodes preserve POI themes."""
        from app.services.vrp_utils import build_problem

        maut = {
            "places": [{"id": "poi1", "name": "Museum", "roles": ["attraction"],
                       "coordinates": {"lat": 1.3, "lng": 103.8}, "themes": ["cultural_history", "history"]}],
            "meta": {"num_days": 1},
        }
        hotel = {"id": "hotel1", "name": "Hotel", "lat": 1.3, "lon": 103.8}

        _, nodes, _ = build_problem(maut, hotel, pacing="balanced")
        attraction_nodes = [n for n in nodes if n.role == "attraction"]
        assert len(attraction_nodes) > 0
        assert attraction_nodes[0].themes == ["cultural_history", "history"]
