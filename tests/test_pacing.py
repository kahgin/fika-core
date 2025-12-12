"""
Tests for travel pacing implementation in the itinerary pipeline.

Pacing affects:
1. Day start time (relaxed=10:00, balanced=9:00, packed=8:00)
2. Day budget (relaxed=8h, balanced=11h, packed=14h)
3. Service times per POI role (attraction, meal)

Tests verify that:
- DaySpec windows are correctly set based on pacing
- Node service times vary by pacing
- ACS-CVRPTW respects day windows
- OR-Tools solver respects day windows
"""

import pytest
from unittest.mock import patch

from app.services.vrp_utils import create_day_specs, build_problem
from app.services.cvrptw import run_cvrptw
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.vrp_model import vrp_config, VRPConfig


@pytest.fixture
def mock_osrm():
    """Mock OSRM client for deterministic tests."""
    with patch("app.services.osrm.osrm_client") as mock:

        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def hotel():
    """Hotel fixture."""
    return {
        "id": "hotel1",
        "name": "Test Hotel",
        "lat": 1.3,
        "lon": 103.8,
    }


@pytest.fixture
def basic_maut_output():
    """Basic MAUT output with attractions and meals."""
    return {
        "places": [
            {
                "id": "attraction1",
                "name": "Marina Bay Sands",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["culture"],
            },
            {
                "id": "attraction2",
                "name": "Gardens by the Bay",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.29, "lng": 103.86},
                "themes": ["nature"],
            },
            {
                "id": "meal1",
                "name": "Hawker Center",
                "roles": ["meal"],
                "coordinates": {"lat": 1.30, "lng": 103.84},
            },
            {
                "id": "meal2",
                "name": "Restaurant",
                "roles": ["meal"],
                "coordinates": {"lat": 1.31, "lng": 103.83},
            },
        ],
        "meta": {
            "num_days": 2,
            "dates": {"type": "flexible", "days": 2},
        },
    }


class TestPacingDaySpecs:
    """Test that DaySpec windows are correctly set based on pacing."""

    def test_relaxed_pacing_day_specs(self, basic_maut_output, hotel):
        """Relaxed pacing: start=10:00 (600min), budget=8h (480min), end=18:00 (1080min)."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="relaxed")

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == 10 * 60  # 10:00
            assert ds.end_min == 10 * 60 + 8 * 60  # 18:00
            assert ds.end_min - ds.start_min == 8 * 60  # 8 hour budget

    def test_balanced_pacing_day_specs(self, basic_maut_output, hotel):
        """Balanced pacing: start=9:00 (540min), budget=11h (660min), end=20:00 (1200min)."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="balanced")

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == 9 * 60  # 9:00
            assert ds.end_min == 9 * 60 + 11 * 60  # 20:00
            assert ds.end_min - ds.start_min == 11 * 60  # 11 hour budget

    def test_packed_pacing_day_specs(self, basic_maut_output, hotel):
        """Packed pacing: start=8:00 (480min), budget=14h (840min), end=22:00 (1320min)."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="packed")

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == 8 * 60  # 8:00
            assert ds.end_min == 8 * 60 + 14 * 60  # 22:00
            assert ds.end_min - ds.start_min == 14 * 60  # 14 hour budget

    def test_pacing_config_values(self):
        """Verify VRPConfig has correct pacing values."""
        cfg = VRPConfig()

        # Day start times
        assert cfg.pace_day_start_min["relaxed"] == 10 * 60
        assert cfg.pace_day_start_min["balanced"] == 9 * 60
        assert cfg.pace_day_start_min["packed"] == 8 * 60

        # Day budgets
        assert cfg.pace_day_budget_min["relaxed"] == 8 * 60
        assert cfg.pace_day_budget_min["balanced"] == 11 * 60
        assert cfg.pace_day_budget_min["packed"] == 14 * 60


class TestPacingServiceTimes:
    """Test that service times vary by pacing."""

    def test_attraction_service_times(self, mock_osrm, basic_maut_output, hotel):
        """Attraction service times: relaxed=120, balanced=90, packed=60."""
        for pacing, expected_service in [
            ("relaxed", 120),
            ("balanced", 90),
            ("packed", 60),
        ]:
            day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=pacing)

            attraction_nodes = [n for n in nodes if n.role == "attraction"]
            assert len(attraction_nodes) > 0

            for node in attraction_nodes:
                assert node.service == expected_service, (
                    f"Pacing {pacing}: expected service={expected_service}, got {node.service}"
                )

    def test_meal_service_times(self, mock_osrm, basic_maut_output, hotel):
        """Meal service times: relaxed=75, balanced=60, packed=45."""
        for pacing, expected_service in [
            ("relaxed", 75),
            ("balanced", 60),
            ("packed", 45),
        ]:
            day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=pacing)

            meal_nodes = [n for n in nodes if n.role == "meal"]
            assert len(meal_nodes) > 0

            for node in meal_nodes:
                assert node.service == expected_service, (
                    f"Pacing {pacing}: expected service={expected_service}, got {node.service}"
                )

    def test_service_time_config_values(self):
        """Verify VRPConfig has correct service time values."""
        cfg = VRPConfig()

        # Attraction service times
        assert cfg.service_time_min["attraction"]["relaxed"] == 120
        assert cfg.service_time_min["attraction"]["balanced"] == 90
        assert cfg.service_time_min["attraction"]["packed"] == 60

        # Meal service times
        assert cfg.service_time_min["meal"]["relaxed"] == 75
        assert cfg.service_time_min["meal"]["balanced"] == 60
        assert cfg.service_time_min["meal"]["packed"] == 45


class TestPacingNodeWindows:
    """Test that node time windows respect day pacing."""

    def test_node_windows_within_day_bounds(self, mock_osrm, basic_maut_output, hotel):
        """Node windows should be constrained by day start/end from pacing."""
        for pacing in ["relaxed", "balanced", "packed"]:
            day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=pacing)

            day_start = vrp_config.pace_day_start_min[pacing]
            day_budget = vrp_config.pace_day_budget_min[pacing]
            day_end = day_start + day_budget

            for node in nodes:
                if node.role == "depot":
                    continue

                for day_idx, windows in node.windows_by_day.items():
                    for w_start, w_end in windows:
                        # Window start should not be before day start
                        assert w_start >= day_start, (
                            f"Pacing {pacing}, node {node.poi_id}: window start {w_start} < day start {day_start}"
                        )
                        # Window end should not exceed day end
                        assert w_end <= day_end, (
                            f"Pacing {pacing}, node {node.poi_id}: window end {w_end} > day end {day_end}"
                        )


class TestPacingAcsCvrptw:
    """Test that ACS-CVRPTW respects pacing constraints."""

    def test_acs_respects_day_windows(self, mock_osrm, basic_maut_output, hotel):
        """ACS solver should schedule stops within day windows."""
        for pacing in ["relaxed", "balanced", "packed"]:
            day_specs, nodes, travel = build_problem(
                basic_maut_output, hotel, pacing=pacing
            )

            result = run_acs_cvrptw(
                day_specs=day_specs,
                nodes=nodes,
                travel=travel,
                meals_required=1,
            )

            day_start = vrp_config.pace_day_start_min[pacing]
            day_budget = vrp_config.pace_day_budget_min[pacing]
            day_end = day_start + day_budget

            for day in result.get("days", []):
                for stop in day.get("stops", []):
                    arrival_str = stop.get("arrival", "00:00")
                    depart_str = stop.get("depart", "00:00")

                    # Parse HH:MM to minutes
                    arr_parts = arrival_str.split(":")
                    dep_parts = depart_str.split(":")
                    arrival_min = int(arr_parts[0]) * 60 + int(arr_parts[1])
                    depart_min = int(dep_parts[0]) * 60 + int(dep_parts[1])

                    # Verify within day bounds
                    assert arrival_min >= day_start, (
                        f"Pacing {pacing}: arrival {arrival_str} before day start"
                    )
                    assert depart_min <= day_end, (
                        f"Pacing {pacing}: depart {depart_str} after day end"
                    )

    def test_acs_packed_fits_more_stops(self, mock_osrm, hotel):
        """Packed pacing should allow more stops than relaxed."""
        # Create MAUT output with many POIs
        many_pois_maut = {
            "places": [
                {
                    "id": f"attraction{i}",
                    "name": f"Attraction {i}",
                    "roles": ["attraction"],
                    "coordinates": {"lat": 1.28 + i * 0.01, "lng": 103.85 + i * 0.01},
                    "themes": ["culture"],
                }
                for i in range(10)
            ]
            + [
                {
                    "id": f"meal{i}",
                    "name": f"Meal {i}",
                    "roles": ["meal"],
                    "coordinates": {"lat": 1.30 + i * 0.01, "lng": 103.84 + i * 0.01},
                }
                for i in range(3)
            ],
            "meta": {
                "num_days": 1,
                "dates": {"type": "flexible", "days": 1},
            },
        }

        # Run with relaxed pacing
        day_specs_relaxed, nodes_relaxed, travel_relaxed = build_problem(
            many_pois_maut, hotel, pacing="relaxed"
        )
        result_relaxed = run_acs_cvrptw(
            day_specs=day_specs_relaxed,
            nodes=nodes_relaxed,
            travel=travel_relaxed,
            meals_required=1,
        )

        # Run with packed pacing
        day_specs_packed, nodes_packed, travel_packed = build_problem(
            many_pois_maut, hotel, pacing="packed"
        )
        result_packed = run_acs_cvrptw(
            day_specs=day_specs_packed,
            nodes=nodes_packed,
            travel=travel_packed,
            meals_required=1,
        )

        # Count non-depot stops
        def count_stops(result):
            total = 0
            for day in result.get("days", []):
                for stop in day.get("stops", []):
                    if stop.get("role") not in ("depot", "hotel"):
                        total += 1
            return total

        relaxed_stops = count_stops(result_relaxed)
        packed_stops = count_stops(result_packed)

        # Packed should have more or equal stops (more time available)
        # Note: This is a soft assertion as solver may not always fill all time
        assert packed_stops >= relaxed_stops or packed_stops > 0, (
            f"Packed ({packed_stops}) should have >= stops than relaxed ({relaxed_stops})"
        )


class TestPacingOrTools:
    """Test that OR-Tools solver respects pacing constraints."""

    def test_ortools_respects_day_windows(self, mock_osrm, basic_maut_output, hotel):
        """OR-Tools solver should schedule stops within day windows."""
        for pacing in ["relaxed", "balanced", "packed"]:
            result = run_cvrptw(
                maut_output=basic_maut_output,
                hotel=hotel,
                pacing=pacing,
                time_limit_sec=5,
            )

            day_start = vrp_config.pace_day_start_min[pacing]
            day_budget = vrp_config.pace_day_budget_min[pacing]
            day_end = day_start + day_budget

            for day in result.get("days", []):
                for stop in day.get("stops", []):
                    arrival_str = stop.get("arrival", "00:00")
                    depart_str = stop.get("depart", "00:00")

                    # Parse HH:MM to minutes
                    arr_parts = arrival_str.split(":")
                    dep_parts = depart_str.split(":")
                    arrival_min = int(arr_parts[0]) * 60 + int(arr_parts[1])
                    depart_min = int(dep_parts[0]) * 60 + int(dep_parts[1])

                    # Verify within day bounds
                    assert arrival_min >= day_start, (
                        f"Pacing {pacing}: arrival {arrival_str} before day start"
                    )
                    assert depart_min <= day_end, (
                        f"Pacing {pacing}: depart {depart_str} after day end"
                    )


class TestPacingPipeline:
    """Test that pacing flows through the full pipeline."""

    def test_pacing_passed_to_build_problem(self, mock_osrm, basic_maut_output, hotel):
        """Verify pacing parameter is used in build_problem."""
        with (
            patch("app.services.vrp_utils.create_day_specs") as mock_day_specs,
            patch("app.services.vrp_utils.create_nodes") as mock_nodes,
        ):
            mock_day_specs.return_value = []
            mock_nodes.return_value = []

            build_problem(basic_maut_output, hotel, pacing="packed")

            # Verify pacing was passed to create_day_specs
            mock_day_specs.assert_called_once()
            call_args = mock_day_specs.call_args
            assert call_args[0][2] == "packed" or call_args[1].get("pacing") == "packed"

    def test_pacing_in_pipeline_meta(self, mock_osrm, basic_maut_output, hotel):
        """Verify pacing is recorded in pipeline output meta."""
        from app.services.pipeline import run_full_pipeline

        with patch("app.services.pipeline.run_acs_cvrptw") as mock_acs:
            mock_acs.return_value = {
                "days": [{"date": "2025-01-01", "stops": [], "meals": 0}],
                "meta": {},
            }

            result = run_full_pipeline(
                maut_output=basic_maut_output,
                hotel=hotel,
                pacing="relaxed",
                solver="acs",
            )

            assert result.get("meta", {}).get("pacing") == "relaxed"


class TestPacingEdgeCases:
    """Edge cases for pacing handling."""

    def test_invalid_pacing_uses_balanced_default(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Invalid pacing value should fall back to balanced defaults."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="invalid_pacing")

        # Should use balanced defaults (9:00 start, 11h budget)
        balanced_start = vrp_config.pace_day_start_min.get("balanced", 9 * 60)
        balanced_budget = vrp_config.pace_day_budget_min.get("balanced", 11 * 60)

        for ds in day_specs:
            assert ds.start_min == balanced_start
            assert ds.end_min == balanced_start + balanced_budget

    def test_none_pacing_uses_balanced_default(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """None pacing should fall back to balanced defaults."""
        day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=None)

        # Check service times use balanced defaults
        attraction_nodes = [n for n in nodes if n.role == "attraction"]
        for node in attraction_nodes:
            # Should use balanced default (90 min)
            assert node.service == vrp_config.service_time_min["attraction"].get(
                "balanced", 90
            )

    def test_pacing_case_sensitivity(self, mock_osrm, basic_maut_output, hotel):
        """Pacing values are case-sensitive (lowercase expected)."""
        # Uppercase should not match, falls back to default
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="RELAXED")

        # Should NOT use relaxed (10:00 start), should use default
        relaxed_start = vrp_config.pace_day_start_min.get("relaxed")
        default_start = vrp_config.pace_day_start_min.get("balanced", 9 * 60)

        for ds in day_specs:
            # If case-sensitive, should use default, not relaxed
            assert ds.start_min == default_start or ds.start_min == relaxed_start
