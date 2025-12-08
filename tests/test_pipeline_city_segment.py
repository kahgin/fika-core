from app.services.pipeline import (
    segment_by_city,
    allocate_days_per_city,
    select_hotel_for_city,
    validate_global_rules,
)


class TestSegmentByCity:
    """Tests for segment_by_city function."""

    def test_segment_by_city_area_name_fallback(self):
        """Test segmentation with area_name fallback."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Place 1",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "poi2",
                    "name": "Place 2",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.35, "lng": 103.85},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        assert "Johor" in result
        assert "Singapore" in result

    def test_segment_by_city_clustering_fallback(self):
        """Test segmentation with KMeans clustering for POIs without admin fields."""
        maut_output = {
            "places": [
                {
                    "id": f"poi{i}",
                    "name": f"Place {i}",
                    "coordinates": {"lat": 1.3 + i * 0.01, "lng": 103.8 + i * 0.01},
                    "poi_roles": ["attraction"],
                }
                for i in range(10)
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        # Should create cluster groups
        assert len(result) >= 1
        total_pois = sum(len(city["places"]) for city in result.values())
        assert total_pois == 10

    def test_segment_by_city_mixed(self):
        """Test segmentation with mixed admin fields and clustering."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Place 1",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "poi2",
                    "name": "Place 2",
                    "coordinates": {"lat": 1.31, "lng": 103.81},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        assert "Singapore" in result
        # The uncategorized POI should be in a cluster
        total_pois = sum(len(city["places"]) for city in result.values())
        assert total_pois == 2

class TestAllocateDaysPerCity:
    """Tests for allocate_days_per_city function."""

    def test_allocate_days_by_capacity(self):
        """Test day allocation by capacity estimation."""
        maut_suboutput = {
            "places": [{"id": f"poi{i}"} for i in range(16)],
            "meta": {"area_name": "Singapore"},
        }

        days = allocate_days_per_city(maut_suboutput)

        # 16 POIs / 6 capacity = 2.67 -> ceil = 3
        assert days == 3

    def test_allocate_days_user_specified(self):
        """Test day allocation with user-specified days."""
        maut_suboutput = {
            "places": [{"id": f"poi{i}"} for i in range(16)],
            "meta": {"area_name": "Singapore"},
        }
        user_input = {"days_per_city": {"Singapore": 5}}

        days = allocate_days_per_city(maut_suboutput, user_input)

        assert days == 5

    def test_allocate_days_minimum_one(self):
        """Test that minimum 1 day is allocated for cities with POIs."""
        maut_suboutput = {
            "places": [{"id": "poi1"}],
            "meta": {"area_name": "Singapore"},
        }

        days = allocate_days_per_city(maut_suboutput)

        assert days >= 1

    def test_allocate_days_empty_city(self):
        """Test that 0 days allocated for empty cities."""
        maut_suboutput = {
            "places": [],
            "meta": {"area_name": "Singapore"},
        }

        days = allocate_days_per_city(maut_suboutput)

        assert days == 0

    def test_allocate_days_large_city_capacity(self):
        """Test capacity adjustment for large cities."""
        maut_suboutput = {
            "places": [{"id": f"poi{i}"} for i in range(10)],
            "meta": {"area_name": "Tokyo", "city_population": 14_000_000},
        }

        days = allocate_days_per_city(maut_suboutput)

        # 10 POIs / 5 capacity (large city) = 2
        assert days == 2

    def test_allocate_days_from_meta_num_days(self):
        """Test day allocation from meta.num_days."""
        maut_suboutput = {
            "places": [{"id": f"poi{i}"} for i in range(16)],
            "meta": {"area_name": "Singapore", "num_days": 4},
        }

        days = allocate_days_per_city(maut_suboutput)

        assert days == 4


class TestSelectHotelForCity:
    """Tests for select_hotel_for_city function."""

    def test_select_hotel_maut_ranking(self):
        """Test hotel selection by MAUT score."""
        maut_suboutput = {
            "places": [
                {
                    "id": "hotel1",
                    "name": "Budget Hotel",
                    "poi_roles": ["accommodation"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "_score": 0.5,
                },
                {
                    "id": "hotel2",
                    "name": "Luxury Hotel",
                    "poi_roles": ["accommodation"],
                    "coordinates": {"lat": 1.31, "lng": 103.81},
                    "_score": 0.9,
                },
            ],
            "meta": {"area_name": "Singapore"},
        }

        hotel = select_hotel_for_city(maut_suboutput, 3)

        assert hotel["id"] == "hotel2"
        assert hotel["name"] == "Luxury Hotel"
        assert hotel["source"] == "maut"

    def test_select_hotel_user_provided(self):
        """Test hotel selection with user-provided hotel."""
        maut_suboutput = {
            "places": [],
            "meta": {"area_name": "Singapore"},
        }
        user_hotels = {
            "Singapore": {
                "id": "user_hotel",
                "name": "My Hotel",
                "lat": 1.3,
                "lon": 103.8,
            }
        }

        hotel = select_hotel_for_city(maut_suboutput, 3, user_hotels)

        assert hotel["id"] == "user_hotel"
        assert hotel["source"] == "user"

    def test_select_hotel_none_available(self):
        """Test error when no accommodation available."""
        maut_suboutput = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Attraction",
                    "poi_roles": ["attraction"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                }
            ],
            "meta": {"area_name": "Singapore"},
        }

        hotel = select_hotel_for_city(maut_suboutput, 3)

        assert hotel["status"] == "error"
        assert hotel["error"] == "no_accommodation"

    def test_select_hotel_invalid_coords(self):
        """Test error when hotel has invalid coordinates."""
        maut_suboutput = {
            "places": [],
            "meta": {"area_name": "Singapore"},
        }
        user_hotels = {
            "Singapore": {
                "id": "user_hotel",
                "name": "My Hotel",
                "lat": None,
                "lon": 103.8,
            }
        }

        hotel = select_hotel_for_city(maut_suboutput, 3, user_hotels)

        assert hotel["status"] == "error"
        assert hotel["error"] == "invalid_hotel_coords"

    def test_select_hotel_from_meta_selected(self):
        """Test hotel selection from meta.selected_hotel."""
        maut_suboutput = {
            "places": [],
            "meta": {
                "area_name": "Singapore",
                "selected_hotel": {
                    "id": "meta_hotel",
                    "name": "Meta Selected Hotel",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                },
            },
        }

        hotel = select_hotel_for_city(maut_suboutput, 3)

        assert hotel["id"] == "meta_hotel"
        assert hotel["source"] == "maut_selected"


class TestValidateGlobalRules:
    """Tests for validate_global_rules function."""

    def test_validate_global_rules_meals(self):
        """Test meal count validation."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},
                        {"role": "meal"},  # 4 meals exceeds default max of 3
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert any("meals" in e.lower() for e in validation["errors"])

    def test_validate_global_rules_themes(self):
        """Test theme repetition validation."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "attraction", "themes": ["culture"]},  # 3 same theme
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert any("theme" in e.lower() for e in validation["errors"])

    def test_validate_global_rules_mandatory(self):
        """Test mandatory POI validation."""
        result = {
            "days": [{"stops": []}],
            "meta": {"missed_mandatory": ["poi1", "poi2"]},
        }

        validation = validate_global_rules(result)

        assert not validation["ok"]
        assert any("mandatory" in e.lower() for e in validation["errors"])

    def test_validate_global_rules_pass(self):
        """Test validation passes for valid result."""
        result = {
            "days": [
                {
                    "stops": [
                        {"role": "attraction", "themes": ["culture"]},
                        {"role": "meal"},
                        {"role": "attraction", "themes": ["nature"]},
                        {"role": "meal"},
                    ]
                }
            ],
            "meta": {},
        }

        validation = validate_global_rules(result)

        assert validation["ok"]
        assert len(validation["errors"]) == 0
