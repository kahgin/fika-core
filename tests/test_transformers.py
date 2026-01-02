"""Tests for data transformers - frontend ↔ backend conversion."""

from app.services.transformers import (
    transform_frontend_payload,
    transform_poi_to_frontend,
    calculate_num_days,
)


class TestCalculateNumDays:
    """Tests for calculate_num_days function."""

    def test_flexible_days(self):
        assert calculate_num_days({"dates": {"type": "flexible", "days": 5}}) == 5

    def test_flexible_clamped_max(self):
        assert calculate_num_days({"dates": {"type": "flexible", "days": 15}}) == 10

    def test_flexible_clamped_min(self):
        assert calculate_num_days({"dates": {"type": "flexible", "days": 0}}) == 1

    def test_specific_dates(self):
        payload = {"dates": {"type": "specific", "start_date": "2025-01-15", "end_date": "2025-01-17"}}
        assert calculate_num_days(payload) == 3

    def test_missing_dates_default(self):
        assert calculate_num_days({}) == 3


class TestTransformFrontendPayload:
    """Tests for transform_frontend_payload function."""

    def _base(self):
        return {
            "destinations": [{"city": "Singapore"}],
            "dates": {"type": "flexible", "days": 3},
            "preferences": {"pacing": "balanced", "interests": ["cultural_history"]},
            "flags": {},
        }

    def test_basic_transformation(self):
        result = transform_frontend_payload(self._base())
        assert result["destination"] == "Singapore"
        assert result["num_days"] == 3
        assert result["pacing"] == "balanced"
        assert result["interest_themes"] == ["cultural_history"]

    def test_muslim_flag_excludes_nightlife(self):
        payload = self._base()
        payload["flags"] = {"is_muslim": True}
        result = transform_frontend_payload(payload)
        assert "nightlife" in result["excluded_themes"]

    def test_muslim_flag_adds_halal(self):
        payload = self._base()
        payload["flags"] = {"is_muslim": True}
        payload["dietary_restrictions"] = []
        result = transform_frontend_payload(payload)
        assert "halal" in result["dietary_restrictions"]

    def test_dietary_string_to_list(self):
        payload = self._base()
        payload["dietary_restrictions"] = "vegetarian"
        result = transform_frontend_payload(payload)
        assert result["dietary_restrictions"] == ["vegetarian"]

    def test_excluded_themes_deduped(self):
        payload = self._base()
        payload["excluded_themes"] = ["nightlife", "family", "nightlife"]
        result = transform_frontend_payload(payload)
        assert result["excluded_themes"] == ["nightlife", "family"]

    def test_default_destination(self):
        result = transform_frontend_payload({"dates": {"type": "flexible", "days": 3}, "preferences": {}, "flags": {}})
        assert result["destination"] == "Singapore"


class TestTransformPoiToFrontend:
    """Tests for transform_poi_to_frontend function."""

    def test_basic_transform(self):
        poi = {
            "id": "poi1",
            "name": "Test POI",
            "roles": ["attraction"],
            "themes": ["cultural"],
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }
        result = transform_poi_to_frontend(poi)
        assert result["id"] == "poi1"
        assert result["name"] == "Test POI"
        assert result["roles"] == ["attraction"]
