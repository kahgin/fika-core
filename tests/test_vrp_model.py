"""Tests for VRP model classes and configuration."""

import datetime as dt
from app.services.vrp_model import VRPConfig, DaySpec, Node, vrp_config


class TestVRPConfig:
    """Tests for VRPConfig values."""

    def test_default_config_exists(self):
        assert vrp_config is not None
        assert isinstance(vrp_config, VRPConfig)

    def test_pacing_values_defined(self):
        for pacing in ["relaxed", "balanced", "packed"]:
            assert pacing in vrp_config.pace_day_start_min
            assert pacing in vrp_config.pace_day_budget_min

    def test_service_times_defined(self):
        for role in ["attraction", "meal"]:
            assert role in vrp_config.service_time_min
            for pacing in ["relaxed", "balanced", "packed"]:
                assert pacing in vrp_config.service_time_min[role]

    def test_meal_windows_defined(self):
        assert len(vrp_config.meal_windows) == 3  # breakfast, lunch, dinner

    def test_penalties_defined(self):
        assert vrp_config.mandatory_miss_penalty > vrp_config.drop_poi_penalty


class TestDaySpec:
    """Tests for DaySpec dataclass."""

    def test_creation(self):
        ds = DaySpec(day_index=0, date=dt.date(2025, 1, 15),
                     start_min=9*60, end_min=20*60, depot_id="hotel1")
        assert ds.day_index == 0
        assert ds.start_min == 9 * 60
        assert ds.end_min == 20 * 60


class TestNode:
    """Tests for Node dataclass."""

    def test_creation(self):
        node = Node(idx=0, poi_id="poi1", name="Test POI", role="attraction",
                    lat=1.3, lon=103.8, service=60, themes=["cultural"],
                    windows_by_day={0: [(10*60, 18*60)]})
        assert node.poi_id == "poi1"
        assert node.service == 60

    def test_mandatory_defaults_false(self):
        node = Node(idx=0, poi_id="poi1", name="Test", role="attraction",
                    lat=1.3, lon=103.8, service=60, themes=None,
                    windows_by_day={0: [(10*60, 18*60)]})
        assert node.is_mandatory is False
