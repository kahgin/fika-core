"""Tests for build_problem function - depot, nodes, and travel matrix."""

import pytest
from unittest.mock import patch
from app.services.vrp_utils import build_problem


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
    return {"id": "hotel1", "name": "Test Hotel", "lat": 1.3, "lon": 103.8}


@pytest.fixture
def maut_output():
    return {
        "places": [
            {
                "id": "attraction1",
                "name": "Marina Bay",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["cultural_history"],
            },
            {"id": "meal1", "name": "Hawker Center", "roles": ["meal"], "coordinates": {"lat": 1.29, "lng": 103.84}},
        ],
        "meta": {"num_days": 2, "dates": {"type": "flexible", "days": 2}},
    }


class TestBuildProblem:
    """Core tests for build_problem function."""

    def test_depot_node_created(self, mock_osrm, maut_output, hotel):
        """Depot node is created correctly from hotel."""
        day_specs, nodes, _ = build_problem(maut_output, hotel, pacing="balanced")
        depot = nodes[0]
        assert depot.idx == 0
        assert depot.poi_id == hotel["id"]
        assert depot.role == "depot"
        assert depot.lat == hotel["lat"]

    def test_day_specs_match_num_days(self, mock_osrm, maut_output, hotel):
        """Day specs count matches num_days."""
        day_specs, _, _ = build_problem(maut_output, hotel, pacing="balanced")
        assert len(day_specs) == 2
        for ds in day_specs:
            assert ds.depot_id == hotel["id"]

    def test_travel_matrix_square(self, mock_osrm, maut_output, hotel):
        """Travel matrix is square with 0 diagonal."""
        _, nodes, travel = build_problem(maut_output, hotel, pacing="balanced")
        n = len(nodes)
        assert len(travel) == n
        for i, row in enumerate(travel):
            assert len(row) == n
            assert travel[i][i] == 0

    def test_pacing_affects_windows(self, mock_osrm, maut_output, hotel):
        """Packed pacing has longer day windows than relaxed."""
        ds_relaxed, _, _ = build_problem(maut_output, hotel, pacing="relaxed")
        ds_packed, _, _ = build_problem(maut_output, hotel, pacing="packed")
        relaxed_dur = ds_relaxed[0].end_min - ds_relaxed[0].start_min
        packed_dur = ds_packed[0].end_min - ds_packed[0].start_min
        assert packed_dur > relaxed_dur

    def test_single_day_no_hotel_events(self, mock_osrm, hotel):
        """Single-day trip has no hotel events."""
        maut = {
            "places": [{"id": "a1", "name": "A", "roles": ["attraction"], "coordinates": {"lat": 1.3, "lng": 103.8}}],
            "meta": {"num_days": 1},
        }
        day_specs, nodes, _ = build_problem(maut, hotel, is_first_city=True, is_last_city=True)
        assert day_specs[0].has_hotel_event is False
        accommodation_nodes = [n for n in nodes if n.role == "accommodation" and n.is_mandatory]
        assert len(accommodation_nodes) == 0

    def test_multi_day_has_hotel_events(self, mock_osrm, maut_output, hotel):
        """Multi-day trip has check-in and check-out."""
        day_specs, nodes, _ = build_problem(maut_output, hotel, is_first_city=True, is_last_city=True)
        assert day_specs[0].has_check_in is True
        assert day_specs[-1].has_check_out is True

    def test_mandatory_poi_flagged(self, mock_osrm, maut_output, hotel):
        """Mandatory POIs are flagged correctly."""
        mandatory = {"attraction1": {"day": 1, "window": ["10:00", "12:00"]}}
        _, nodes, _ = build_problem(maut_output, hotel, mandatory=mandatory)
        mand_nodes = [n for n in nodes if n.is_mandatory and n.role not in ("depot", "accommodation")]
        assert len(mand_nodes) > 0

    def test_skips_poi_without_coords(self, mock_osrm, hotel):
        """POIs without coordinates are skipped."""
        maut = {
            "places": [
                {"id": "no_coords", "name": "X", "roles": ["attraction"]},
                {"id": "has_coords", "name": "Y", "roles": ["attraction"], "coordinates": {"lat": 1.3, "lng": 103.8}},
            ],
            "meta": {"num_days": 1},
        }
        _, nodes, _ = build_problem(maut, hotel)
        poi_ids = [n.poi_id for n in nodes]
        assert not any("no_coords" in pid for pid in poi_ids)
        assert any("has_coords" in pid for pid in poi_ids)
