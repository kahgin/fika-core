import pytest
from unittest.mock import patch
from app.services.pipeline import run_full_pipeline


@pytest.fixture
def mock_osrm():
    """Mock OSRM client for deterministic tests."""
    with patch("app.services.osrm.osrm_client") as mock:
        # Mock distance to return haversine-like values
        mock.distance.return_value = 5.0  # 5 km

        # Mock matrix_minutes to return travel times
        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def single_city_maut():
    """Single city MAUT output fixture."""
    return {
        "places": [
            {
                "id": "hotel1",
                "name": "Test Hotel",
                "poi_roles": ["accommodation"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.3, "lng": 103.8},
                "_score": 0.9,
                "complete_address": {"city": "Singapore"},
            },
            {
                "id": "attraction1",
                "name": "Marina Bay",
                "poi_roles": ["attraction"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["culture"],
                "complete_address": {"city": "Singapore"},
            },
            {
                "id": "meal1",
                "name": "Hawker Center",
                "poi_roles": ["meal"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.29, "lng": 103.84},
                "complete_address": {"city": "Singapore"},
            },
        ],
        "meta": {
            "num_days": 2,
            "dates": {"type": "flexible", "days": 2},
            "selected_themes": ["culture"],
        },
    }


@pytest.fixture
def multi_city_maut():
    """Multi-city MAUT output fixture."""
    return {
        "places": [
            # Singapore POIs
            {
                "id": "sg_hotel",
                "name": "Singapore Hotel",
                "poi_roles": ["accommodation"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.3, "lng": 103.8},
                "_score": 0.9,
                "complete_address": {"city": "Singapore"},
            },
            {
                "id": "sg_attraction1",
                "name": "Marina Bay",
                "poi_roles": ["attraction"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["culture"],
                "complete_address": {"city": "Singapore"},
            },
            {
                "id": "sg_meal1",
                "name": "Hawker Center",
                "poi_roles": ["meal"],
                "area_name": "Singapore",
                "coordinates": {"lat": 1.29, "lng": 103.84},
                "complete_address": {"city": "Singapore"},
            },
            # Kuala Lumpur POIs
            {
                "id": "kl_hotel",
                "name": "KL Hotel",
                "poi_roles": ["accommodation"],
                "area_name": "Kuala Lumpur",
                "coordinates": {"lat": 3.15, "lng": 101.7},
                "_score": 0.85,
                "complete_address": {"city": "Kuala Lumpur"},
            },
            {
                "id": "kl_attraction1",
                "name": "Petronas Towers",
                "poi_roles": ["attraction"],
                "area_name": "Kuala Lumpur",
                "coordinates": {"lat": 3.16, "lng": 101.71},
                "themes": ["architecture"],
                "complete_address": {"city": "Kuala Lumpur"},
            },
            {
                "id": "kl_meal1",
                "name": "Jalan Alor",
                "poi_roles": ["meal"],
                "area_name": "Kuala Lumpur",
                "coordinates": {"lat": 3.14, "lng": 101.69},
                "complete_address": {"city": "Kuala Lumpur"},
            },
        ],
        "meta": {
            "num_days": 4,
            "dates": {"type": "flexible", "days": 4},
            "selected_themes": ["culture", "architecture"],
        },
    }


class TestSingleCityPipeline:
    """Tests for single-city pipeline scenarios."""

    def test_single_city_with_maut_hotel(self, mock_osrm, single_city_maut):
        """Test single city with hotel selected from MAUT."""
        # Add selected_hotel to meta
        single_city_maut["meta"]["selected_hotel"] = {
            "id": "hotel1",
            "name": "Test Hotel",
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }

        result = run_full_pipeline(single_city_maut, solver="acs")

        assert result["status"] == "success"
        assert len(result["days"]) > 0
        assert "meta" in result
        assert result["meta"]["solver"] == "acs"

    def test_single_city_with_explicit_hotel(self, mock_osrm, single_city_maut):
        """Test single city with explicitly provided hotel."""
        hotel = {
            "id": "explicit_hotel",
            "name": "Explicit Hotel",
            "area_name": "Singapore",
            "lat": 1.3,
            "lon": 103.8,
        }

        result = run_full_pipeline(single_city_maut, hotel=hotel, solver="ortools")

        assert result["status"] == "success"
        assert result["meta"]["solver"] == "ortools"

    def test_single_city_no_hotel_error(self, mock_osrm):
        """Test error when no hotel available."""
        maut_output = {
            "places": [
                {
                    "id": "attraction1",
                    "name": "Attraction",
                    "poi_roles": ["attraction"],
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "complete_address": {"city": "Singapore"},
                }
            ],
            "meta": {"num_days": 1},
        }

        result = run_full_pipeline(maut_output, solver="acs")

        assert result["status"] == "error"


class TestMultiCityPipeline:
    """Tests for multi-city pipeline scenarios."""

    def test_two_cities_segmentation(self, mock_osrm, multi_city_maut):
        """Test pipeline segments and processes two cities."""
        result = run_full_pipeline(multi_city_maut, solver="acs")

        # Should succeed or partial_success
        assert result["status"] in ("success", "partial_success")
        assert len(result["days"]) > 0

    def test_multi_city_days_have_area_name(self, mock_osrm, multi_city_maut):
        """Test that each day has area_name field."""
        result = run_full_pipeline(multi_city_maut, solver="acs")

        if result["status"] in ("success", "partial_success"):
            for day in result["days"]:
                assert "area_name" in day

    def test_multi_city_request_id_in_meta(self, mock_osrm, multi_city_maut):
        """Test that request_id is included in meta."""
        result = run_full_pipeline(multi_city_maut, solver="acs")

        assert "request_id" in result["meta"]


class TestPipelineEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_places(self, mock_osrm):
        """Test pipeline with empty places."""
        maut_output = {"places": [], "meta": {"num_days": 1}}

        result = run_full_pipeline(maut_output, solver="acs")

        assert result["status"] == "error"

    def test_too_many_cities(self, mock_osrm):
        """Test pipeline rejects too many cities."""
        # Create 6 cities (exceeds MAX_CITIES_PER_REQUEST=5)
        places = []
        for i in range(6):
            places.append(
                {
                    "id": f"hotel_{i}",
                    "name": f"Hotel {i}",
                    "poi_roles": ["accommodation"],
                    "area_name": f"City{i}",
                    "coordinates": {"lat": 1.0 + i, "lng": 100.0 + i},
                    "complete_address": {"city": f"City{i}"},
                }
            )
            places.append(
                {
                    "id": f"attraction_{i}",
                    "name": f"Attraction {i}",
                    "poi_roles": ["attraction"],
                    "area_name": f"City{i}",
                    "coordinates": {"lat": 1.01 + i, "lng": 100.01 + i},
                    "complete_address": {"city": f"City{i}"},
                }
            )

        maut_output = {
            "places": places,
            "meta": {"num_days": 6},
        }

        result = run_full_pipeline(maut_output, solver="acs")

        assert result["status"] == "error"
        assert "too many" in result["error"].lower() or "request_too_large" in str(
            result
        )

    def test_partial_success_with_failed_cities(self, mock_osrm):
        """Test partial success when some cities fail."""
        maut_output = {
            "places": [
                # City with hotel
                {
                    "id": "sg_hotel",
                    "name": "Singapore Hotel",
                    "poi_roles": ["accommodation"],
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "complete_address": {"city": "Singapore"},
                },
                {
                    "id": "sg_attraction",
                    "name": "Marina Bay",
                    "poi_roles": ["attraction"],
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.28, "lng": 103.85},
                    "complete_address": {"city": "Singapore"},
                },
                # City without hotel (will fail)
                {
                    "id": "kl_attraction",
                    "name": "Petronas",
                    "poi_roles": ["attraction"],
                    "area_name": "Kuala Lumpur",
                    "coordinates": {"lat": 3.15, "lng": 101.7},
                    "complete_address": {"city": "Kuala Lumpur"},
                },
            ],
            "meta": {"num_days": 2},
        }

        result = run_full_pipeline(maut_output, solver="acs")

        # Should be partial_success since KL has no hotel
        if result["status"] == "partial_success":
            assert "failed_cities" in result["meta"]
            assert "Kuala Lumpur" in result["meta"]["failed_cities"]


class TestPipelineValidation:
    """Tests for validation in pipeline."""

    def test_validation_runs_on_result(self, mock_osrm, single_city_maut):
        """Test that validation is run on the result."""
        single_city_maut["meta"]["selected_hotel"] = {
            "id": "hotel1",
            "name": "Test Hotel",
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }

        result = run_full_pipeline(single_city_maut, solver="acs")

        assert result["days"][0]["stops"][-1]["name"] == "Test Hotel"


class TestPipelinePacing:
    """Tests for different pacing options."""

    def test_relaxed_pacing(self, mock_osrm, single_city_maut):
        """Test pipeline with relaxed pacing."""
        single_city_maut["meta"]["selected_hotel"] = {
            "id": "hotel1",
            "name": "Test Hotel",
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }

        result = run_full_pipeline(single_city_maut, pacing="relaxed", solver="acs")

        assert result["meta"]["pacing"] == "relaxed"

    def test_packed_pacing(self, mock_osrm, single_city_maut):
        """Test pipeline with packed pacing."""
        single_city_maut["meta"]["selected_hotel"] = {
            "id": "hotel1",
            "name": "Test Hotel",
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }

        result = run_full_pipeline(single_city_maut, pacing="packed", solver="acs")

        assert result["meta"]["pacing"] == "packed"
