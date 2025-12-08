import pytest
from unittest.mock import patch
from app.services.cvrptw import build_problem


@pytest.fixture
def mock_osrm():
    """Mock OSRM client for deterministic tests."""
    with patch("app.services.cvrptw.osrm_client") as mock:

        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def basic_maut_output():
    """Basic MAUT output fixture."""
    return {
        "places": [
            {
                "id": "attraction1",
                "name": "Marina Bay",
                "poi_roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["culture"],
            },
            {
                "id": "meal1",
                "name": "Hawker Center",
                "poi_roles": ["meal"],
                "coordinates": {"lat": 1.29, "lng": 103.84},
            },
        ],
        "meta": {
            "num_days": 2,
            "dates": {"type": "flexible", "days": 2},
            "selected_themes": ["culture"],
        },
    }


@pytest.fixture
def hotel():
    """Hotel fixture."""
    return {
        "id": "hotel1",
        "name": "Test Hotel",
        "lat": 1.3,
        "lon": 103.8,
    }


class TestBuildProblemDepot:
    """Tests for build_problem depot node handling."""

    def test_build_problem_depot_node(self, mock_osrm, basic_maut_output, hotel):
        """Test that depot node is created correctly with explicit hotel."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            selected_themes=["culture"],
        )

        # Verify depot node (index 0)
        assert len(nodes) > 0
        depot = nodes[0]
        assert depot.idx == 0
        assert depot.poi_id == hotel["id"]
        assert depot.name == hotel["name"]
        assert depot.role == "depot"  # Current implementation uses "depot"
        assert depot.lat == hotel["lat"]
        assert depot.lon == hotel["lon"]

    def test_build_problem_day_specs(self, mock_osrm, basic_maut_output, hotel):
        """Test that day specs are created correctly."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
        )

        assert len(day_specs) == 2  # num_days = 2
        for day_spec in day_specs:
            assert day_spec.depot_id == hotel["id"]
            assert day_spec.start_min > 0
            assert day_spec.end_min > day_spec.start_min

    def test_build_problem_travel_matrix(self, mock_osrm, basic_maut_output, hotel):
        """Test that travel matrix is computed correctly."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
        )

        # Travel matrix should be square
        n = len(nodes)
        assert len(travel) == n
        for row in travel:
            assert len(row) == n

        # Diagonal should be 0
        for i in range(n):
            assert travel[i][i] == 0

    def test_build_problem_pacing_affects_windows(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Test that pacing affects day windows."""
        day_specs_relaxed, _, _ = build_problem(
            basic_maut_output,
            hotel,
            pacing="relaxed",
        )

        day_specs_packed, _, _ = build_problem(
            basic_maut_output,
            hotel,
            pacing="packed",
        )

        # Packed should have longer day windows
        relaxed_duration = day_specs_relaxed[0].end_min - day_specs_relaxed[0].start_min
        packed_duration = day_specs_packed[0].end_min - day_specs_packed[0].start_min
        assert packed_duration > relaxed_duration

    def test_build_problem_nodes_have_windows(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Test that all nodes have time windows."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
        )

        for node in nodes:
            assert hasattr(node, "windows_by_day")
            # Depot should have windows for all days
            if node.idx == 0:
                assert len(node.windows_by_day) == len(day_specs)


class TestBuildProblemMandatory:
    """Tests for mandatory POI handling in build_problem."""

    def test_build_problem_mandatory_flag(self, mock_osrm, basic_maut_output, hotel):
        """Test that mandatory POIs are flagged correctly."""
        mandatory = {"attraction1": {"day": 1, "window": ["10:00", "12:00"]}}

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        # Find the mandatory node
        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) > 0

    def test_build_problem_mandatory_window(self, mock_osrm, basic_maut_output, hotel):
        """Test that mandatory POIs have constrained windows."""
        mandatory = {"attraction1": {"day": 1, "window": ["10:00", "12:00"]}}

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        # Find the mandatory node for day 0 (API is 1-based)
        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) > 0

        # Check window is constrained
        for node in mandatory_nodes:
            if 0 in node.windows_by_day:
                windows = node.windows_by_day[0]
                assert len(windows) == 1
                assert windows[0] == (10 * 60, 12 * 60)  # 10:00-12:00


class TestBuildProblemEdgeCases:
    """Tests for edge cases in build_problem."""

    def test_build_problem_empty_places(self, mock_osrm, hotel):
        """Test build_problem with empty places."""
        maut_output = {
            "places": [],
            "meta": {"num_days": 1},
        }

        day_specs, nodes, travel = build_problem(
            maut_output,
            hotel,
            pacing="balanced",
        )

        # Should have at least depot node
        assert len(nodes) >= 1
        assert nodes[0].role == "depot"

    def test_build_problem_missing_coords(self, mock_osrm, hotel):
        """Test build_problem skips POIs with missing coordinates."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "No Coords",
                    "poi_roles": ["attraction"],
                    # Missing coordinates
                },
                {
                    "id": "poi2",
                    "name": "Has Coords",
                    "poi_roles": ["attraction"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                },
            ],
            "meta": {"num_days": 1},
        }

        day_specs, nodes, travel = build_problem(
            maut_output,
            hotel,
            pacing="balanced",
        )

        # Should only have depot + poi2 (poi1 skipped)
        poi_ids = [n.poi_id for n in nodes]
        assert "poi1_day0" not in poi_ids
        # poi2 should be present (with _day0 suffix)
        assert any("poi2" in pid for pid in poi_ids)

    def test_build_problem_specific_dates(self, mock_osrm, hotel):
        """Test build_problem with specific dates."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Attraction",
                    "poi_roles": ["attraction"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                },
            ],
            "meta": {
                "num_days": 2,
                "dates": {
                    "type": "specific",
                    "startDate": "2025-01-15",
                    "endDate": "2025-01-16",
                },
            },
        }

        day_specs, nodes, travel = build_problem(
            maut_output,
            hotel,
            pacing="balanced",
        )

        assert len(day_specs) == 2
        # Dates should be parsed correctly
        assert day_specs[0].date.isoformat() == "2025-01-15"
        assert day_specs[1].date.isoformat() == "2025-01-16"
