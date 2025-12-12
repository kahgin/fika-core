"""
Tests for data transformers.

Tests:
- Frontend → Backend transformation (transform_frontend_payload)
- Backend → Frontend transformation (transform_poi_to_frontend)
- Naming convention enforcement
"""

from app.services.transformers import (
    transform_frontend_payload,
    transform_poi_to_frontend,
    calculate_num_days,
)


class TestCalculateNumDays:
    """Tests for calculate_num_days function."""

    def test_flexible_days(self):
        """Test flexible date type uses days field."""
        payload = {"dates": {"type": "flexible", "days": 5}}
        assert calculate_num_days(payload) == 5

    def test_flexible_days_clamped_max(self):
        """Test flexible days clamped to max 10."""
        payload = {"dates": {"type": "flexible", "days": 15}}
        assert calculate_num_days(payload) == 10

    def test_flexible_days_clamped_min(self):
        """Test flexible days clamped to min 1."""
        payload = {"dates": {"type": "flexible", "days": 0}}
        assert calculate_num_days(payload) == 1

    def test_specific_dates(self):
        """Test specific date type calculates from range."""
        payload = {
            "dates": {
                "type": "specific",
                "start_date": "2025-01-15",
                "end_date": "2025-01-17",
            }
        }
        assert calculate_num_days(payload) == 3  # 15, 16, 17

    def test_specific_dates_single_day(self):
        """Test specific dates same day = 1 day."""
        payload = {
            "dates": {
                "type": "specific",
                "start_date": "2025-01-15",
                "end_date": "2025-01-15",
            }
        }
        assert calculate_num_days(payload) == 1

    def test_missing_dates_default(self):
        """Test missing dates defaults to 3."""
        payload = {}
        assert calculate_num_days(payload) == 3

    def test_invalid_dates_default(self):
        """Test invalid dates defaults to 3."""
        payload = {"dates": {"type": "specific", "start_date": "invalid"}}
        assert calculate_num_days(payload) == 3


class TestTransformFrontendPayload:
    """Tests for transform_frontend_payload function."""

    def test_basic_transformation(self):
        """Test basic payload transformation."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {"pacing": "balanced", "interests": ["culture"]},
            "flags": {},
        }

        result = transform_frontend_payload(payload)

        assert result["destination"] == "Singapore"
        assert result["num_days"] == 3
        assert result["pacing"] == "balanced"
        assert result["interest_themes"] == ["culture"]

    def test_muslim_flag_excludes_nightlife(self):
        """Test is_muslim flag adds nightlife to excluded themes."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {"is_muslim": True},
        }

        result = transform_frontend_payload(payload)

        assert "nightlife" in result["excluded_themes"]

    def test_muslim_flag_adds_halal(self):
        """Test is_muslim flag adds halal to dietary restrictions."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {"is_muslim": True},
            "dietary_restrictions": [],
        }

        result = transform_frontend_payload(payload)

        assert "halal" in result["dietary_restrictions"]

    def test_dietary_string_to_list(self):
        """Test dietary restrictions string converted to list."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
            "dietary_restrictions": "vegetarian",
        }

        result = transform_frontend_payload(payload)

        assert result["dietary_restrictions"] == ["vegetarian"]

    def test_dietary_none_to_empty(self):
        """Test dietary restrictions 'none' converted to empty list."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
            "dietary_restrictions": "none",
        }

        result = transform_frontend_payload(payload)

        assert result["dietary_restrictions"] == []

    def test_excluded_themes_deduped(self):
        """Test excluded themes are deduplicated."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
            "excluded_themes": ["nightlife", "family", "nightlife"],
        }

        result = transform_frontend_payload(payload)

        assert result["excluded_themes"] == ["nightlife", "family"]

    def test_multi_destination_uses_first(self):
        """Test multi-destination uses first city."""
        payload = {
            "destinations": [{"city": "Singapore"}, {"city": "Johor"}],
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
        }

        result = transform_frontend_payload(payload)

        assert result["destination"] == "Singapore"

    def test_default_destination(self):
        """Test default destination when none provided."""
        payload = {
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
        }

        result = transform_frontend_payload(payload)

        assert result["destination"] == "Singapore"

    def test_budget_tier_extracted(self):
        """Test budget tier is extracted from preferences."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {"budget": "luxury"},
            "flags": {},
        }

        result = transform_frontend_payload(payload)

        assert result["budget_tier"] == "luxury"

    def test_seed_coordinates_passed(self):
        """Test seed coordinates are passed through."""
        payload = {
            "destination": "Singapore",
            "dates": {"type": "flexible", "days": 3},
            "preferences": {},
            "flags": {},
            "seed_lon": 103.8,
            "seed_lat": 1.3,
        }

        result = transform_frontend_payload(payload)

        assert result["seed_lon"] == 103.8
        assert result["seed_lat"] == 1.3


class TestTransformPoiToFrontend:
    """Tests for transform_poi_to_frontend function."""

    def test_basic_transformation(self):
        """Test basic POI transformation to frontend format."""
        poi = {
            "id": "poi1",
            "name": "Marina Bay",
            "roles": ["attraction"],
            "themes": ["culture"],
            "rating": 4.5,
            "review_count": 1000,
            "coordinates": {"lat": 1.3, "lng": 103.8},
            "open_hours": {"Monday": ["10 am-9 pm"]},
            "price_level": 2,
        }

        result = transform_poi_to_frontend(poi)

        assert result["id"] == "poi1"
        assert result["name"] == "Marina Bay"
        assert result["roles"] == ["attraction"]
        assert result["themes"] == ["culture"]
        assert result["rating"] == 4.5
        assert result["reviewCount"] == 1000
        assert result["openHours"] == {"Monday": ["10 am-9 pm"]}
        assert result["priceLevel"] == 2

    def test_snake_to_camel_case(self):
        """Test snake_case fields converted to camelCase."""
        poi = {
            "id": "poi1",
            "name": "Test",
            "review_count": 500,
            "open_hours": {},
            "price_level": 1,
            "roles": [],
        }

        result = transform_poi_to_frontend(poi)

        # Should have camelCase keys
        assert "reviewCount" in result
        assert "openHours" in result
        assert "priceLevel" in result

        # Should NOT have snake_case keys
        assert "review_count" not in result
        assert "open_hours" not in result
        assert "price_level" not in result

    def test_review_rating_to_rating(self):
        """Test review_rating mapped to rating."""
        poi = {
            "id": "poi1",
            "name": "Test",
            "review_rating": 4.2,
        }

        result = transform_poi_to_frontend(poi)

        assert result["rating"] == 4.2

    def test_category_from_categories(self):
        """Test category extracted from categories list."""
        poi = {
            "id": "poi1",
            "name": "Test",
            "categories": ["restaurant", "cafe"],
        }

        result = transform_poi_to_frontend(poi)

        assert result["category"] == "restaurant"
        assert result["categories"] == ["restaurant", "cafe"]

    def test_coordinates_preserved(self):
        """Test coordinates are preserved."""
        poi = {
            "id": "poi1",
            "name": "Test",
            "coordinates": {"lat": 1.3, "lng": 103.8},
        }

        result = transform_poi_to_frontend(poi)

        assert result["coordinates"] == {"lat": 1.3, "lng": 103.8}

    def test_missing_fields_handled(self):
        """Test missing fields don't cause errors."""
        poi = {
            "id": "poi1",
            "name": "Test",
        }

        result = transform_poi_to_frontend(poi)

        assert result["id"] == "poi1"
        assert result["name"] == "Test"
        # Missing fields should be None or empty
        assert result.get("rating") is None
        assert result.get("reviewCount") is None


class TestNamingConventions:
    """Tests for naming convention utilities."""

    def test_to_camel_case(self):
        """Test snake_case to camelCase conversion."""
        from app.services.transformers import to_camel_case

        assert to_camel_case("open_hours") == "openHours"
        assert to_camel_case("review_count") == "reviewCount"
        assert to_camel_case("price_level") == "priceLevel"
        assert to_camel_case("roles") == "roles"
        assert to_camel_case("single") == "single"

    def test_naming_utils_available(self):
        """Test naming utilities are available."""
        from app.utils.naming import (
            to_camel_case,
            to_snake_case,
        )

        assert to_camel_case("test_field") == "testField"
        assert to_snake_case("testField") == "test_field"

    def test_dict_to_camel_case(self):
        """Test dict keys converted to camelCase."""
        from app.utils.naming import dict_to_camel_case

        data = {
            "open_hours": {"Monday": ["10 am"]},
            "price_level": 2,
            "nested_data": {"inner_key": "value"},
        }

        result = dict_to_camel_case(data)

        assert "openHours" in result
        assert "priceLevel" in result
        assert "nestedData" in result
        assert "innerKey" in result["nestedData"]

    def test_dict_to_snake_case(self):
        """Test dict keys converted to snake_case."""
        from app.utils.naming import dict_to_snake_case

        data = {
            "openHours": {"Monday": ["10 am"]},
            "priceLevel": 2,
        }

        result = dict_to_snake_case(data)

        assert "open_hours" in result
        assert "price_level" in result
