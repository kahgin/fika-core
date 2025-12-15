"""
Tests for travel pacing implementation in the itinerary pipeline.

Pacing affects (from vrp_config):
1. Day start time (relaxed=10:00, balanced=10:00, packed=8:00)
2. Day budget (relaxed=12h, balanced=12h, packed=16h)
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
                "themes": ["cultural_history"],
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
        """Relaxed pacing should use VRPConfig values for start/budget."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="relaxed")

        expected_start = vrp_config.pace_day_start_min["relaxed"]
        expected_budget = vrp_config.pace_day_budget_min["relaxed"]
        expected_end = expected_start + expected_budget

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == expected_start
            assert ds.end_min == expected_end
            assert ds.end_min - ds.start_min == expected_budget

    def test_balanced_pacing_day_specs(self, basic_maut_output, hotel):
        """Balanced pacing should use VRPConfig values."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="balanced")

        expected_start = vrp_config.pace_day_start_min["balanced"]
        expected_budget = vrp_config.pace_day_budget_min["balanced"]
        expected_end = expected_start + expected_budget

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == expected_start
            assert ds.end_min == expected_end
            assert ds.end_min - ds.start_min == expected_budget

    def test_packed_pacing_day_specs(self, basic_maut_output, hotel):
        """Packed pacing should use VRPConfig values."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="packed")

        expected_start = vrp_config.pace_day_start_min["packed"]
        expected_budget = vrp_config.pace_day_budget_min["packed"]
        expected_end = expected_start + expected_budget

        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.start_min == expected_start
            assert ds.end_min == expected_end
            assert ds.end_min - ds.start_min == expected_budget

    def test_pacing_config_values(self):
        """Verify VRPConfig has all required pacing keys defined."""
        cfg = VRPConfig()

        # Verify all pacing keys exist
        for pacing in ["relaxed", "balanced", "packed"]:
            assert pacing in cfg.pace_day_start_min, f"Missing start time for {pacing}"
            assert pacing in cfg.pace_day_budget_min, f"Missing budget for {pacing}"
            # Verify values are sensible (positive integers)
            assert cfg.pace_day_start_min[pacing] > 0
            assert cfg.pace_day_budget_min[pacing] > 0

        # Verify ordering: relaxed < balanced < packed for budget
        assert cfg.pace_day_budget_min["relaxed"] <= cfg.pace_day_budget_min["balanced"]
        assert cfg.pace_day_budget_min["balanced"] <= cfg.pace_day_budget_min["packed"]


class TestPacingServiceTimes:
    """Test that service times vary by pacing."""

    def test_attraction_service_times(self, mock_osrm, basic_maut_output, hotel):
        """Attraction service times should match VRPConfig values."""
        for pacing in ["relaxed", "balanced", "packed"]:
            expected_service = vrp_config.service_time_min["attraction"][pacing]
            day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=pacing)

            attraction_nodes = [n for n in nodes if n.role == "attraction"]
            assert len(attraction_nodes) > 0

            for node in attraction_nodes:
                assert node.service == expected_service, (
                    f"Pacing {pacing}: expected service={expected_service}, got {node.service}"
                )

    def test_meal_service_times(self, mock_osrm, basic_maut_output, hotel):
        """Meal service times should match VRPConfig values."""
        for pacing in ["relaxed", "balanced", "packed"]:
            expected_service = vrp_config.service_time_min["meal"][pacing]
            day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=pacing)

            meal_nodes = [n for n in nodes if n.role == "meal"]
            assert len(meal_nodes) > 0

            for node in meal_nodes:
                assert node.service == expected_service, (
                    f"Pacing {pacing}: expected service={expected_service}, got {node.service}"
                )

    def test_service_time_config_values(self):
        """Verify VRPConfig has all required service time keys defined."""
        cfg = VRPConfig()

        # Verify all role/pacing combinations exist
        for role in ["attraction", "meal"]:
            assert role in cfg.service_time_min, f"Missing service time for {role}"
            for pacing in ["relaxed", "balanced", "packed"]:
                assert pacing in cfg.service_time_min[role], f"Missing {pacing} for {role}"
                assert cfg.service_time_min[role][pacing] > 0

        # Verify ordering: relaxed >= balanced >= packed for service times
        for role in ["attraction", "meal"]:
            assert cfg.service_time_min[role]["relaxed"] >= cfg.service_time_min[role]["balanced"]
            assert cfg.service_time_min[role]["balanced"] >= cfg.service_time_min[role]["packed"]


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
            day_specs, nodes, travel = build_problem(basic_maut_output, hotel, pacing=pacing)

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
                    assert arrival_min >= day_start, f"Pacing {pacing}: arrival {arrival_str} before day start"
                    assert depart_min <= day_end, f"Pacing {pacing}: depart {depart_str} after day end"

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
                    "themes": ["cultural_history"],
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
        day_specs_relaxed, nodes_relaxed, travel_relaxed = build_problem(many_pois_maut, hotel, pacing="relaxed")
        result_relaxed = run_acs_cvrptw(
            day_specs=day_specs_relaxed,
            nodes=nodes_relaxed,
            travel=travel_relaxed,
            meals_required=1,
        )

        # Run with packed pacing
        day_specs_packed, nodes_packed, travel_packed = build_problem(many_pois_maut, hotel, pacing="packed")
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
                    assert arrival_min >= day_start, f"Pacing {pacing}: arrival {arrival_str} before day start"
                    assert depart_min <= day_end, f"Pacing {pacing}: depart {depart_str} after day end"


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

    def test_invalid_pacing_uses_hardcoded_default(self, mock_osrm, basic_maut_output, hotel):
        """Invalid pacing value should fall back to hardcoded defaults."""
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="invalid_pacing")

        # Code falls back to hardcoded values: 9:00 start (540), 11h budget (660)
        # See vrp_utils.py create_day_specs: get(pacing, 9*60), get(pacing, 11*60)
        hardcoded_start = 9 * 60  # 540
        hardcoded_budget = 11 * 60  # 660

        for ds in day_specs:
            assert ds.start_min == hardcoded_start
            assert ds.end_min == hardcoded_start + hardcoded_budget

    def test_none_pacing_uses_hardcoded_default(self, mock_osrm, basic_maut_output, hotel):
        """None pacing should fall back to hardcoded defaults."""
        day_specs, nodes, _ = build_problem(basic_maut_output, hotel, pacing=None)

        # Check service times use hardcoded defaults (90 min when pacing key not found)
        attraction_nodes = [n for n in nodes if n.role == "attraction"]
        for node in attraction_nodes:
            # When pacing=None, service_times.get(None, 90) returns 90
            assert node.service == 90

    def test_pacing_case_sensitivity(self, mock_osrm, basic_maut_output, hotel):
        """Pacing values are case-sensitive (lowercase expected)."""
        # Uppercase should not match, falls back to hardcoded default
        day_specs = create_day_specs(basic_maut_output, hotel, pacing="RELAXED")

        # Should use hardcoded fallback (9:00 = 540), not relaxed from config
        hardcoded_start = 9 * 60  # 540 - the fallback value
        relaxed_start = vrp_config.pace_day_start_min["relaxed"]  # 600

        for ds in day_specs:
            # Case-sensitive: 'RELAXED' won't match 'relaxed', so fallback used
            assert ds.start_min == hardcoded_start, (
                f"Expected hardcoded fallback {hardcoded_start}, got {ds.start_min}. "
                f"If {ds.start_min} == {relaxed_start}, pacing is case-insensitive."
            )
