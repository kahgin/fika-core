"""Integration tests for multi-city pipeline."""

import pytest
from unittest.mock import patch
from app.services.pipeline import run_full_pipeline


@pytest.fixture
def mock_osrm():
    with patch("app.services.osrm.osrm_client") as mock:
        mock.distance.return_value = 5.0
        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]
        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def single_city_maut():
    return {
        "places": [
            {"id": "hotel1", "name": "Hotel", "roles": ["accommodation"],
             "area_name": "Singapore", "coordinates": {"lat": 1.3, "lng": 103.8}, "_score": 0.9},
            {"id": "a1", "name": "Marina Bay", "roles": ["attraction"],
             "area_name": "Singapore", "coordinates": {"lat": 1.28, "lng": 103.85}, "themes": ["cultural"]},
            {"id": "m1", "name": "Hawker", "roles": ["meal"],
             "area_name": "Singapore", "coordinates": {"lat": 1.29, "lng": 103.84}},
        ],
        "meta": {"num_days": 2, "dates": {"type": "flexible", "days": 2}},
    }


@pytest.fixture
def multi_city_maut():
    return {
        "places": [
            {"id": "sg_hotel", "name": "SG Hotel", "roles": ["accommodation"],
             "area_name": "Singapore", "coordinates": {"lat": 1.3, "lng": 103.8}, "_score": 0.9},
            {"id": "sg_a1", "name": "Marina Bay", "roles": ["attraction"],
             "area_name": "Singapore", "coordinates": {"lat": 1.28, "lng": 103.85}, "themes": ["cultural"]},
            {"id": "kl_hotel", "name": "KL Hotel", "roles": ["accommodation"],
             "area_name": "Kuala Lumpur", "coordinates": {"lat": 3.15, "lng": 101.7}, "_score": 0.85},
            {"id": "kl_a1", "name": "Petronas", "roles": ["attraction"],
             "area_name": "Kuala Lumpur", "coordinates": {"lat": 3.16, "lng": 101.71}, "themes": ["architecture"]},
        ],
        "meta": {"num_days": 4, "dates": {"type": "flexible", "days": 4}},
    }


class TestPipelineBasic:
    """Basic pipeline tests."""

    def test_single_city_success(self, mock_osrm, single_city_maut):
        result = run_full_pipeline(single_city_maut, solver="acs")
        assert result["status"] in ("success", "partial_success")
        assert len(result["days"]) > 0

    def test_multi_city_segments(self, mock_osrm, multi_city_maut):
        result = run_full_pipeline(multi_city_maut, solver="acs")
        assert result["status"] in ("success", "partial_success")

    def test_days_have_area_name(self, mock_osrm, multi_city_maut):
        result = run_full_pipeline(multi_city_maut, solver="acs")
        if result["status"] in ("success", "partial_success"):
            for day in result["days"]:
                assert "area_name" in day


class TestPipelineEdgeCases:
    """Edge case tests."""

    def test_empty_places_error(self, mock_osrm):
        result = run_full_pipeline({"places": [], "meta": {"num_days": 1}}, solver="acs")
        assert result["status"] == "error"

    def test_too_many_cities_error(self, mock_osrm):
        places = []
        for i in range(6):
            places.extend([
                {"id": f"h{i}", "name": f"Hotel{i}", "roles": ["accommodation"],
                 "area_name": f"City{i}", "coordinates": {"lat": 1+i, "lng": 100+i}},
                {"id": f"a{i}", "name": f"Attr{i}", "roles": ["attraction"],
                 "area_name": f"City{i}", "coordinates": {"lat": 1.01+i, "lng": 100.01+i}},
            ])
        result = run_full_pipeline({"places": places, "meta": {"num_days": 6}}, solver="acs")
        assert result["status"] == "error"


class TestPipelinePacing:
    """Pacing tests."""

    def test_pacing_in_meta(self, mock_osrm, single_city_maut):
        for pacing in ["relaxed", "balanced", "packed"]:
            result = run_full_pipeline(single_city_maut, pacing=pacing, solver="acs")
            assert result["meta"]["pacing"] == pacing
