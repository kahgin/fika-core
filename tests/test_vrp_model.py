"""
Tests for VRP model classes and configuration.

Tests:
- VRPConfig values
- DaySpec creation
- Node creation
"""

import datetime as dt

from app.services.vrp_model import VRPConfig, DaySpec, Node, vrp_config


class TestVRPConfig:
    """Tests for VRPConfig dataclass."""

    def test_default_config_exists(self):
        """Test default vrp_config is available."""
        assert vrp_config is not None
        assert isinstance(vrp_config, VRPConfig)

    def test_pacing_day_start(self):
        """Test pacing day start times are defined."""
        assert hasattr(vrp_config, "pace_day_start_min")
        assert "relaxed" in vrp_config.pace_day_start_min
        assert "balanced" in vrp_config.pace_day_start_min
        assert "packed" in vrp_config.pace_day_start_min

    def test_pacing_day_budget(self):
        """Test pacing day budgets are defined."""
        assert hasattr(vrp_config, "pace_day_budget_min")
        assert "relaxed" in vrp_config.pace_day_budget_min
        assert "balanced" in vrp_config.pace_day_budget_min
        assert "packed" in vrp_config.pace_day_budget_min

    def test_service_times_defined(self):
        """Test service times are defined for roles."""
        assert hasattr(vrp_config, "service_time_min")
        assert "attraction" in vrp_config.service_time_min
        assert "meal" in vrp_config.service_time_min

    def test_service_times_by_pacing(self):
        """Test service times vary by pacing."""
        attraction_times = vrp_config.service_time_min["attraction"]
        assert "relaxed" in attraction_times
        assert "balanced" in attraction_times
        assert "packed" in attraction_times

    def test_meal_windows_defined(self):
        """Test meal windows are defined."""
        assert hasattr(vrp_config, "meal_windows")
        assert len(vrp_config.meal_windows) == 3

    def test_penalties_defined(self):
        """Test penalties are defined."""
        assert hasattr(vrp_config, "penalty_meal_to_meal")
        assert hasattr(vrp_config, "penalty_same_theme")
        assert hasattr(vrp_config, "drop_poi_penalty")
        assert hasattr(vrp_config, "mandatory_miss_penalty")

    def test_mandatory_penalty_higher_than_drop(self):
        """Test mandatory miss penalty is higher than drop penalty."""
        assert vrp_config.mandatory_miss_penalty > vrp_config.drop_poi_penalty

    def test_acs_parameters_defined(self):
        """Test ACS algorithm parameters are defined."""
        assert hasattr(vrp_config, "acs_n_ants")
        assert hasattr(vrp_config, "acs_n_iterations")
        assert hasattr(vrp_config, "acs_alpha")
        assert hasattr(vrp_config, "acs_beta")
        assert hasattr(vrp_config, "acs_evaporation_rate")
        assert hasattr(vrp_config, "acs_q")

    def test_default_role_windows_defined(self):
        """Test default role windows are defined."""
        assert hasattr(vrp_config, "default_role_windows")
        assert "attraction" in vrp_config.default_role_windows
        assert "meal" in vrp_config.default_role_windows


class TestDaySpec:
    """Tests for DaySpec dataclass."""

    def test_day_spec_creation(self):
        """Test DaySpec can be created."""
        day_spec = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
        )

        assert day_spec.day_index == 0
        assert day_spec.date == dt.date(2025, 1, 15)
        assert day_spec.start_min == 9 * 60
        assert day_spec.end_min == 20 * 60
        assert day_spec.depot_id == "hotel1"

    def test_day_spec_duration(self):
        """Test day spec duration calculation."""
        day_spec = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
        )

        duration = day_spec.end_min - day_spec.start_min
        assert duration == 11 * 60  # 11 hours


class TestNode:
    """Tests for Node dataclass."""

    def test_node_creation(self):
        """Test Node can be created."""
        node = Node(
            idx=0,
            poi_id="poi1",
            name="Test POI",
            role="attraction",
            lat=1.3,
            lon=103.8,
            service=60,
            themes=["cultural_history"],
            windows_by_day={0: [(10 * 60, 18 * 60)]},
        )

        assert node.idx == 0
        assert node.poi_id == "poi1"
        assert node.name == "Test POI"
        assert node.role == "attraction"
        assert node.lat == 1.3
        assert node.lon == 103.8
        assert node.service == 60
        assert node.themes == ["cultural_history"]

    def test_node_mandatory_default(self):
        """Test Node is_mandatory defaults to False."""
        node = Node(
            idx=0,
            poi_id="poi1",
            name="Test POI",
            role="attraction",
            lat=1.3,
            lon=103.8,
            service=60,
            themes=None,
            windows_by_day={},
        )

        assert node.is_mandatory is False

    def test_node_mandatory_explicit(self):
        """Test Node is_mandatory can be set."""
        node = Node(
            idx=0,
            poi_id="poi1",
            name="Test POI",
            role="attraction",
            lat=1.3,
            lon=103.8,
            service=60,
            themes=None,
            windows_by_day={},
            is_mandatory=True,
        )

        assert node.is_mandatory is True

    def test_node_windows_by_day(self):
        """Test Node windows_by_day structure."""
        node = Node(
            idx=0,
            poi_id="poi1",
            name="Test POI",
            role="attraction",
            lat=1.3,
            lon=103.8,
            service=60,
            themes=None,
            windows_by_day={
                0: [(10 * 60, 12 * 60), (14 * 60, 18 * 60)],
                1: [(10 * 60, 18 * 60)],
            },
        )

        assert 0 in node.windows_by_day
        assert 1 in node.windows_by_day
        assert len(node.windows_by_day[0]) == 2  # Two windows on day 0
        assert len(node.windows_by_day[1]) == 1  # One window on day 1

    def test_depot_node(self):
        """Test depot node creation."""
        depot = Node(
            idx=0,
            poi_id="hotel1",
            name="Hotel",
            role="depot",
            lat=1.3,
            lon=103.8,
            service=0,
            themes=None,
            windows_by_day={0: [(9 * 60, 20 * 60)]},
        )

        assert depot.role == "depot"
        assert depot.service == 0

    def test_meal_node(self):
        """Test meal node creation."""
        meal = Node(
            idx=1,
            poi_id="restaurant1",
            name="Restaurant",
            role="meal",
            lat=1.31,
            lon=103.81,
            service=45,
            themes=["food"],
            windows_by_day={0: [(12 * 60, 14 * 60)]},
        )

        assert meal.role == "meal"
        assert meal.service == 45


class TestConfigValues:
    """Tests for specific config value ranges."""

    def test_acs_alpha_range(self):
        """Test ACS alpha is in valid range."""
        assert 0 < vrp_config.acs_alpha <= 5

    def test_acs_beta_range(self):
        """Test ACS beta is in valid range."""
        assert 0 < vrp_config.acs_beta <= 10

    def test_evaporation_rate_range(self):
        """Test evaporation rate is in valid range."""
        assert 0 < vrp_config.acs_evaporation_rate < 1

    def test_meal_tolerance_positive(self):
        """Test meal tolerance is positive."""
        assert vrp_config.meal_hard_tol_min >= 0

    def test_theme_diversity_bonus_defined(self):
        """Test theme diversity bonus is defined and positive."""
        assert hasattr(vrp_config, "theme_diversity_bonus")
        assert vrp_config.theme_diversity_bonus >= 0

    def test_theme_concentration_penalty_defined(self):
        """Test theme concentration penalty is defined and positive."""
        assert hasattr(vrp_config, "theme_concentration_penalty")
        assert vrp_config.theme_concentration_penalty >= 0
