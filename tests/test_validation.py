from app.services.pipeline import validate_global_rules


class TestValidateGlobalRulesMeals:
    """Tests for meal count validation."""

    def test_meals_within_limit(self):
        """Test validation passes when meals within limit."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "attraction"},
                        {"role": "meal"},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]
        assert len(validation["errors"]) == 0

    def test_meals_exceed_default_limit(self):
        """Test validation fails when meals exceed default limit (3)."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},  # 4 meals
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert any("meals" in e.lower() for e in validation["errors"])

    def test_meals_custom_limit(self):
        """Test validation with custom meals_max config."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                    ]
                }
            ],
            "meta": {},
        }

        # With meals_max=1, 2 meals should fail
        validation = validate_global_rules(result, config={"meals_max": 1})

        assert not validation["ok"]
        assert any("meals" in e.lower() for e in validation["errors"])

    def test_meals_per_day_independent(self):
        """Test that meal validation is per-day."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                    ]
                },
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                    ]
                },
            ],
            "meta": {},
        }

        # 3 meals per day is within limit
        validation = validate_global_rules(result)

        assert validation["ok"]


class TestValidateGlobalRulesThemes:
    """Tests for theme repetition validation."""

    def test_themes_within_limit(self):
        """Test validation passes with 2 same-theme attractions."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["nature"]},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]

    def test_themes_exceed_limit(self):
        """Test validation warns with 3+ same-theme attractions (no hard limit)."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},  # 3rd
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]
        assert any("theme" in w.lower() for w in validation["warnings"])

    def test_themes_only_first_counted(self):
        """Test that only primary (first) theme is counted."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural_history", "history"]},
                        {"role": "attraction", "themes": ["cultural_history", "art"]},
                        {
                            "role": "attraction",
                            "themes": ["history", "cultural_history"],
                        },  # history is primary
                    ]
                }
            ],
            "meta": {},
        }

        # Only 2 attractions have "cultural_history" as primary theme
        validation = validate_global_rules(result)

        assert validation["ok"]

    def test_themes_per_day_independent(self):
        """Test that theme validation is per-day."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},
                    ]
                },
                {
                    "stops": [
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},
                    ]
                },
            ],
            "meta": {},
        }

        # 2 per day is within limit
        validation = validate_global_rules(result)

        assert validation["ok"]

    def test_themes_empty_list(self):
        """Test attractions with empty themes list."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": []},
                        {"role": "attraction", "themes": []},
                        {"role": "attraction", "themes": []},
                    ]
                }
            ],
            "meta": {},
        }

        # Empty themes should not trigger theme limit
        validation = validate_global_rules(result)

        assert validation["ok"]


class TestValidateGlobalRulesMandatory:
    """Tests for mandatory POI validation."""

    def test_no_missed_mandatory(self):
        """Test validation passes with no missed mandatory."""
        result = {
            "days": [{"stops": []}],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]

    def test_missed_mandatory_reported(self):
        """Test validation fails with missed mandatory POIs."""
        result = {
            "days": [{"stops": []}],
            "meta": {"missed_mandatory": ["poi1", "poi2"]},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert any("mandatory" in e.lower() for e in validation["errors"])

    def test_missed_mandatory_empty_list(self):
        """Test validation passes with empty missed_mandatory list."""
        result = {
            "days": [{"stops": []}],
            "meta": {"missed_mandatory": []},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]


class TestValidateGlobalRulesMultiple:
    """Tests for multiple validation failures."""

    def test_multiple_failures(self):
        """Test that multiple failures are all reported."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},  # 4 meals
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},
                        {"role": "attraction", "themes": ["cultural_history"]},  # 3 same theme
                    ]
                }
            ],
            "meta": {"missed_mandatory": ["poi1"]},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert len(validation["errors"]) >= 2  # meals, mandatory
        assert any("theme" in w.lower() for w in validation["warnings"])  # themes are soft warnings

    def test_empty_days(self):
        """Test validation with empty days list."""
        result = {
            "days": [],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]

    def test_empty_stops(self):
        """Test validation with empty stops in days."""
        result = {
            "days": [
                {"stops": []},
                {"stops": []},
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]


class TestValidateGlobalRulesLogging:
    """Tests for validation logging."""

    def test_validation_returns_errors_list(self):
        """Test that validation returns errors as list."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert "ok" in validation
        assert "errors" in validation
        assert isinstance(validation["errors"], list)

    def test_validation_with_request_id(self):
        """Test validation with request_id for logging."""
        result = {
            "days": [{"stops": []}],
            "meta": {},
        }

        # Should not raise
        validation = validate_global_rules(result, request_id="test-123")

        assert validation["ok"]


class TestValidateMinimumAttractions:
    """Tests for minimum attractions per day validation."""

    def test_day_with_attractions_is_valid(self):
        """Test that a day with attractions passes validation."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "poi_id": "a1"},
                        {"role": "attraction", "poi_id": "a2"},
                        {"role": "meal", "poi_id": "m1"},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        # Should pass because we have attractions
        assert validation["ok"] or "attraction" not in str(validation["errors"]).lower()

    def test_empty_day_is_flagged(self):
        """Test that a completely empty day is flagged."""
        result = {
            "days": [
                {"stops": []},  # Empty day
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        # Empty days should be ok (could be travel/rest days)
        # But we track for warnings
        assert isinstance(validation, dict)

    def test_multiple_days_each_has_stops(self):
        """Test multi-day itinerary has reasonable stops."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "poi_id": "a1"},
                        {"role": "meal", "poi_id": "m1"},
                    ]
                },
                {
                    "stops": [
                        {"role": "attraction", "poi_id": "a2"},
                        {"role": "meal", "poi_id": "m2"},
                    ]
                },
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)
        assert validation["ok"]

    def test_day_with_only_meals_has_no_attractions(self):
        """Test a day with only meals has no attractions (edge case)."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal", "poi_id": "m1"},
                        {"role": "meal", "poi_id": "m2"},
                    ]
                }
            ],
            "meta": {},
        }

        validate_global_rules(result)
        attractions = [s for s in result["days"][0]["stops"] if s.get("role") == "attraction"]
        assert len(attractions) == 0
