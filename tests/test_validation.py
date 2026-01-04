"""Tests for validation rules - meals, themes, and hotel events."""

from app.services.pipeline import validate_global_rules


class TestMealValidation:
    """Tests for meal count validation."""

    def test_meals_within_limit(self):
        result = {"days": [{"stops": [{"role": "meal"}, {"role": "attraction"}, {"role": "meal"}]}], "meta": {}}
        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_meals_exceed_limit(self):
        result = {"days": [{"stops": [{"role": "meal"}] * 4}], "meta": {}}
        validation = validate_global_rules(result)
        assert not validation["ok"]
        assert any("meals" in e.lower() for e in validation["errors"])

    def test_meals_custom_limit(self):
        result = {"days": [{"stops": [{"role": "meal"}, {"role": "meal"}]}], "meta": {}}
        validation = validate_global_rules(result, config={"meals_max": 1})
        assert not validation["ok"]


class TestThemeValidation:
    """Tests for theme repetition validation."""

    def test_themes_within_limit(self):
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural"]},
                        {"role": "attraction", "themes": ["cultural"]},
                    ]
                }
            ],
            "meta": {},
        }
        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_themes_exceed_limit_warns(self):
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural"]},
                        {"role": "attraction", "themes": ["cultural"]},
                        {"role": "attraction", "themes": ["cultural"]},
                    ]
                }
            ],
            "meta": {},
        }
        validation = validate_global_rules(result)
        assert validation["ok"]
        assert any("theme" in w.lower() for w in validation["warnings"])


class TestHotelEventValidation:
    """Tests for hotel event pairing validation."""

    def test_paired_checkin_checkout(self):
        result = {
            "days": [
                {"stops": [{"role": "accommodation", "poi_id": "h1", "hotel_event_type": "checkin"}]},
                {"stops": [{"role": "accommodation", "poi_id": "h1", "hotel_event_type": "checkout"}]},
            ],
            "meta": {},
        }
        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_transition_day_allowed(self):
        """Checkout + checkin on same day (transition) is allowed."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "accommodation", "poi_id": "h1", "hotel_event_type": "checkout"},
                        {"role": "accommodation", "poi_id": "h2", "hotel_event_type": "checkin"},
                    ]
                }
            ],
            "meta": {},
        }
        validation = validate_global_rules(result)
        assert validation["ok"]
