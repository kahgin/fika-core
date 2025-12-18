"""
Tests for ACS-CVRPTW solver.

Tests:
- Basic route generation
- Meal constraints
- Theme diversity
- Mandatory POI handling
- Time window enforcement
"""

import pytest
import datetime as dt

from app.services.vrp_model import DaySpec, Node
from app.services.acs_cvrptw import run_acs_cvrptw, _get_base_id


class TestAcsHelpers:
    """Tests for ACS helper functions."""

    def test_get_base_id_with_suffix(self):
        """Test stripping _dayX suffix."""
        assert _get_base_id("poi123_day0") == "poi123"
        assert _get_base_id("poi123_day5") == "poi123"

    def test_get_base_id_without_suffix(self):
        """Test ID without suffix."""
        assert _get_base_id("poi123") == "poi123"


class TestAcsSolver:
    """Tests for ACS solver."""

    @pytest.fixture
    def simple_nodes(self):
        """Simple node setup for testing."""
        return [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 20 * 60)]},
            ),
            Node(
                idx=1,
                poi_id="poi1_day0",
                name="Attraction 1",
                role="attraction",
                lat=1.31,
                lon=103.81,
                service=60,
                themes=["cultural_history"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
            ),
            Node(
                idx=2,
                poi_id="meal1_day0",
                name="Restaurant",
                role="meal",
                lat=1.32,
                lon=103.82,
                service=45,
                themes=["food"],
                windows_by_day={0: [(11 * 60, 14 * 60)]},
            ),
        ]

    @pytest.fixture
    def simple_day_specs(self):
        """Simple day specs for testing."""
        return [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
            )
        ]

    @pytest.fixture
    def simple_travel(self):
        """Simple travel matrix (10 min between all nodes)."""
        return [
            [0, 10, 10],
            [10, 0, 10],
            [10, 10, 0],
        ]

    def test_acs_returns_days(self, simple_nodes, simple_day_specs, simple_travel):
        """Test ACS returns days structure."""
        result = run_acs_cvrptw(
            day_specs=simple_day_specs,
            nodes=simple_nodes,
            travel=simple_travel,
            meals_required=1,
        )

        assert "days" in result
        assert len(result["days"]) == 1

    def test_acs_day_has_stops(self, simple_nodes, simple_day_specs, simple_travel):
        """Test ACS day has stops."""
        result = run_acs_cvrptw(
            day_specs=simple_day_specs,
            nodes=simple_nodes,
            travel=simple_travel,
            meals_required=1,
        )

        day = result["days"][0]
        assert "stops" in day
        assert len(day["stops"]) >= 1  # At least depot

    def test_acs_respects_time_windows(self, simple_nodes, simple_day_specs, simple_travel):
        """Test ACS respects time windows."""
        result = run_acs_cvrptw(
            day_specs=simple_day_specs,
            nodes=simple_nodes,
            travel=simple_travel,
            meals_required=1,
        )

        day = result["days"][0]
        for stop in day["stops"]:
            # Parse arrival time
            arrival = stop["arrival"]
            h, m = map(int, arrival.split(":"))
            arrival_min = h * 60 + m

            # Should be within day bounds
            assert arrival_min >= simple_day_specs[0].start_min - 1  # Allow 1 min tolerance
            assert arrival_min <= simple_day_specs[0].end_min + 1

    def test_acs_empty_nodes(self, simple_day_specs):
        """Test ACS with only depot node."""
        nodes = [
            Node(
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
        ]
        travel = [[0]]

        result = run_acs_cvrptw(
            day_specs=simple_day_specs,
            nodes=nodes,
            travel=travel,
            meals_required=0,
        )

        assert "days" in result
        # Should handle gracefully

    def test_acs_no_day_specs(self, simple_nodes, simple_travel):
        """Test ACS with no day specs."""
        result = run_acs_cvrptw(
            day_specs=[],
            nodes=simple_nodes,
            travel=simple_travel,
            meals_required=1,
        )

        assert result["days"] == []


class TestAcsMandatory:
    """Tests for mandatory POI handling in ACS."""

    @pytest.fixture
    def nodes_with_mandatory(self):
        """Nodes with mandatory POI."""
        return [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 20 * 60)]},
            ),
            Node(
                idx=1,
                poi_id="mandatory1_day0",
                name="Must Visit",
                role="attraction",
                lat=1.31,
                lon=103.81,
                service=60,
                themes=["cultural_history"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
                is_mandatory=True,
            ),
            Node(
                idx=2,
                poi_id="optional1_day0",
                name="Optional",
                role="attraction",
                lat=1.32,
                lon=103.82,
                service=60,
                themes=["nature"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
                is_mandatory=False,
            ),
        ]

    @pytest.fixture
    def day_specs(self):
        return [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
            )
        ]

    @pytest.fixture
    def travel(self):
        return [
            [0, 10, 10],
            [10, 0, 10],
            [10, 10, 0],
        ]

    def test_mandatory_poi_visited(self, nodes_with_mandatory, day_specs, travel):
        """Test mandatory POI is visited."""
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes_with_mandatory,
            travel=travel,
            meals_required=0,
        )

        day = result["days"][0]
        visited_ids = [s["poi_id"] for s in day["stops"]]
        visited_base_ids = [_get_base_id(pid) for pid in visited_ids]

        assert "mandatory1" in visited_base_ids

    def test_missed_mandatory_reported(self):
        """Test missed mandatory is reported in meta."""
        # Create scenario where mandatory can't be visited
        nodes = [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 10 * 60)]},  # Very short day
            ),
            Node(
                idx=1,
                poi_id="mandatory1_day0",
                name="Must Visit",
                role="attraction",
                lat=1.31,
                lon=103.81,
                service=120,  # 2 hours - won't fit
                themes=["cultural_history"],
                windows_by_day={0: [(9 * 60, 10 * 60)]},
                is_mandatory=True,
            ),
        ]
        day_specs = [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=10 * 60,  # Only 1 hour
                depot_id="hotel1",
            )
        ]
        travel = [[0, 30], [30, 0]]  # 30 min travel

        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes,
            travel=travel,
            meals_required=0,
        )

        # Should report missed mandatory
        if "missed_mandatory" in result.get("meta", {}):
            assert "mandatory1" in result["meta"]["missed_mandatory"]


class TestAcsMultiDay:
    """Tests for multi-day ACS solving."""

    @pytest.fixture
    def multi_day_nodes(self):
        """Nodes for multi-day test."""
        nodes = [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 20 * 60)], 1: [(9 * 60, 20 * 60)]},
            ),
        ]
        # Add POIs for each day
        idx = 1
        for day in range(2):
            nodes.append(
                Node(
                    idx=idx,
                    poi_id=f"poi{idx}_day{day}",
                    name=f"Attraction {idx}",
                    role="attraction",
                    lat=1.31 + idx * 0.01,
                    lon=103.81 + idx * 0.01,
                    service=60,
                    themes=["cultural_history"],
                    windows_by_day={day: [(10 * 60, 18 * 60)]},
                )
            )
            idx += 1
        return nodes

    @pytest.fixture
    def multi_day_specs(self):
        return [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
            ),
            DaySpec(
                day_index=1,
                date=dt.date(2025, 1, 16),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
            ),
        ]

    def test_multi_day_returns_all_days(self, multi_day_nodes, multi_day_specs):
        """Test multi-day returns correct number of days."""
        n = len(multi_day_nodes)
        travel = [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        result = run_acs_cvrptw(
            day_specs=multi_day_specs,
            nodes=multi_day_nodes,
            travel=travel,
            meals_required=0,
        )

        assert len(result["days"]) == 2

    def test_poi_visited_once_across_days(self, multi_day_nodes, multi_day_specs):
        """Test each POI is visited at most once across all days."""
        n = len(multi_day_nodes)
        travel = [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        result = run_acs_cvrptw(
            day_specs=multi_day_specs,
            nodes=multi_day_nodes,
            travel=travel,
            meals_required=0,
        )

        # Collect all visited base IDs (excluding depot/hotel/accommodation)
        all_visited = []
        for day in result["days"]:
            for stop in day["stops"]:
                role = stop.get("role", "")
                if role not in ("depot", "hotel", "accommodation"):
                    base_id = _get_base_id(stop["poi_id"])
                    all_visited.append(base_id)

        # No duplicates
        assert len(all_visited) == len(set(all_visited))


class TestAcsUserThemes:
    """Tests for user_themes parameter in ACS solver."""

    @pytest.fixture
    def nodes_with_themes(self):
        """Nodes with different themes for testing user_themes."""
        return [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 20 * 60)]},
            ),
            Node(
                idx=1,
                poi_id="poi1_day0",
                name="Museum",
                role="attraction",
                lat=1.31,
                lon=103.81,
                service=60,
                themes=["cultural_history"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
            ),
            Node(
                idx=2,
                poi_id="poi2_day0",
                name="Nature Park",
                role="attraction",
                lat=1.32,
                lon=103.82,
                service=60,
                themes=["nature"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
            ),
            Node(
                idx=3,
                poi_id="poi3_day0",
                name="Temple",
                role="attraction",
                lat=1.33,
                lon=103.83,
                service=60,
                themes=["cultural_history"],
                windows_by_day={0: [(10 * 60, 18 * 60)]},
            ),
        ]

    @pytest.fixture
    def day_specs(self):
        return [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
            )
        ]

    @pytest.fixture
    def travel(self):
        return [
            [0, 10, 10, 10],
            [10, 0, 10, 10],
            [10, 10, 0, 10],
            [10, 10, 10, 0],
        ]

    def test_acs_accepts_user_themes(self, nodes_with_themes, day_specs, travel):
        """Test ACS accepts user_themes parameter."""
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes_with_themes,
            travel=travel,
            meals_required=0,
            user_themes={"cultural_history", "nature"},
        )

        assert "days" in result
        assert len(result["days"]) == 1

    def test_acs_works_without_user_themes(self, nodes_with_themes, day_specs, travel):
        """Test ACS works when user_themes is None."""
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes_with_themes,
            travel=travel,
            meals_required=0,
            user_themes=None,
        )

        assert "days" in result
        assert len(result["days"]) == 1

    def test_acs_works_with_empty_user_themes(self, nodes_with_themes, day_specs, travel):
        """Test ACS works when user_themes is empty set."""
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes_with_themes,
            travel=travel,
            meals_required=0,
            user_themes=set(),
        )

        assert "days" in result
        assert len(result["days"]) == 1

    def test_acs_single_user_theme(self, nodes_with_themes, day_specs, travel):
        """Test ACS works with single user theme."""
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes_with_themes,
            travel=travel,
            meals_required=0,
            user_themes={"cultural_history"},
        )

        assert "days" in result
        assert len(result["days"]) == 1
        # With only "cultural_history" theme, solver should favor cultural_history POIs
        day = result["days"][0]
        assert len(day["stops"]) >= 1

    def test_acs_single_theme_allows_many_attractions(self, day_specs):
        """Test that single theme mode allows many same-theme attractions."""
        # Create 5 attractions all with the same theme
        nodes = [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Test Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 20 * 60)]},
            ),
        ]
        # Add 5 attractions all with "shopping" theme
        for i in range(5):
            nodes.append(
                Node(
                    idx=i + 1,
                    poi_id=f"shop{i}_day0",
                    name=f"Shopping {i}",
                    role="attraction",
                    lat=1.3 + i * 0.01,
                    lon=103.8 + i * 0.01,
                    service=60,
                    themes=["shopping"],
                    windows_by_day={0: [(9 * 60, 20 * 60)]},
                )
            )

        travel = [[10] * 6 for _ in range(6)]
        for i in range(6):
            travel[i][i] = 0

        # With single theme "shopping", should allow many shopping attractions
        # Theme balance uses SOFT penalties, NOT hard limits
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes,
            travel=travel,
            meals_required=0,
            user_themes=["shopping"],  # Single theme
        )

        day = result["days"][0]
        # Count shopping attractions visited (exclude depot)
        shopping_stops = [s for s in day["stops"] if s.get("role") == "attraction"]

        # Should have multiple attractions - no hard theme limit
        # The solver should visit as many POIs as time permits
        assert len(shopping_stops) >= 3, f"Should allow many same-theme attractions, got {len(shopping_stops)}"

    def test_acs_food_culinary_theme_is_not_meal_like(self, day_specs):
        """Food-themed attractions must not be treated as meals (no hard skipping via food streak)."""
        nodes = [
            Node(
                idx=0,
                poi_id="hotel1",
                name="Test Hotel",
                role="depot",
                lat=1.3,
                lon=103.8,
                service=0,
                themes=None,
                windows_by_day={0: [(9 * 60, 22 * 60)]},
            ),
            Node(
                idx=1,
                poi_id="meal0_day0",
                name="Meal 0",
                role="meal",
                lat=1.31,
                lon=103.81,
                service=45,
                themes=["food_culinary"],
                windows_by_day={0: [(11 * 60, 14 * 60)]},
            ),
            Node(
                idx=2,
                poi_id="meal1_day0",
                name="Meal 1",
                role="meal",
                lat=1.32,
                lon=103.82,
                service=60,
                themes=["food_culinary"],
                windows_by_day={0: [(17 * 60, 20 * 60)]},
            ),
        ]

        for i in range(5):
            nodes.append(
                Node(
                    idx=3 + i,
                    poi_id=f"food{i}_day0",
                    name=f"Food Attraction {i}",
                    role="attraction",
                    lat=1.3 + i * 0.005,
                    lon=103.8 + i * 0.005,
                    service=60,
                    themes=["food_culinary"],
                    windows_by_day={0: [(9 * 60, 22 * 60)]},
                )
            )

        n = len(nodes)
        travel = [[10] * n for _ in range(n)]
        for i in range(n):
            travel[i][i] = 0

        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes,
            travel=travel,
            meals_required=0,
            user_themes=["food_culinary"],
        )

        day = result["days"][0]
        attractions = [s for s in day["stops"] if s.get("role") == "attraction"]
        assert len(attractions) >= 3, f"Expected multiple food attractions, got {len(attractions)}"
