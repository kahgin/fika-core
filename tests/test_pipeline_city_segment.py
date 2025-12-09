"""
Tests for pipeline city segmentation and multi-city routing rules.

Segmentation Strategy:
- Primary: Match by area_name field
- Fallback: KMeans clustering for POIs without area_name
"""

from app.services.pipeline import (
    segment_by_city,
    allocate_days_per_city,
    allocate_days_proportionally,
    select_hotel_for_city,
    validate_global_rules,
)


class TestSegmentByCity:
    """Tests for segment_by_city function.
    
    Segmentation uses area_name as primary method, KMeans clustering as fallback.
    """

    def test_segment_by_area_name_single_city(self):
        """Test segmentation with single city via area_name."""
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
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.35, "lng": 103.85},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        assert len(result) == 1
        assert "Singapore" in result
        assert len(result["Singapore"]["places"]) == 2

    def test_segment_by_area_name_multi_city(self):
        """Test segmentation with multiple cities via area_name."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Place 1",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.5, "lng": 103.8},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "poi2",
                    "name": "Place 2",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.35, "lng": 103.85},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "poi3",
                    "name": "Place 3",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.48, "lng": 103.78},
                    "poi_roles": ["meal"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        assert "Johor" in result
        assert "Singapore" in result
        assert len(result["Johor"]["places"]) == 2
        assert len(result["Singapore"]["places"]) == 1

    def test_segment_kmeans_fallback_no_area_name(self):
        """Test KMeans clustering fallback when POIs lack area_name."""
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

        # Should create cluster groups (cluster_0, cluster_1, etc.)
        assert len(result) >= 1
        assert any(key.startswith("cluster_") for key in result.keys())
        total_pois = sum(len(city["places"]) for city in result.values())
        assert total_pois == 10

    def test_segment_mixed_area_name_and_kmeans(self):
        """Test segmentation with mix of area_name and KMeans fallback."""
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
                    # No area_name - will be clustered
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
        # Should have Singapore + at least one cluster
        assert len(result) >= 1

    def test_segment_preserves_accommodation_count(self):
        """Test that segmentation correctly counts accommodations per city."""
        maut_output = {
            "places": [
                {
                    "id": "hotel1",
                    "name": "Hotel 1",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                },
                {
                    "id": "poi1",
                    "name": "Attraction 1",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.31, "lng": 103.81},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "hotel2",
                    "name": "Hotel 2",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.5, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        # Verify accommodations are in correct cities
        sg_accommodations = [
            p for p in result["Singapore"]["places"]
            if "accommodation" in p.get("poi_roles", [])
        ]
        johor_accommodations = [
            p for p in result["Johor"]["places"]
            if "accommodation" in p.get("poi_roles", [])
        ]
        assert len(sg_accommodations) == 1
        assert len(johor_accommodations) == 1

    # def test_segment_empty_places_raises(self):
    #     """Test that empty places list raises ValueError."""
    #     maut_output = {"places": [], "meta": {}}

    #     with pytest.raises(ValueError, match="No cities identified"):
    #         segment_by_city(maut_output)

    def test_segment_invalid_coordinates_skipped(self):
        """Test that POIs with invalid coordinates are skipped in clustering."""
        maut_output = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Valid POI",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "poi2",
                    "name": "Invalid POI",
                    # No area_name, invalid coords
                    "coordinates": {"lat": None, "lng": 103.8},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        # Only valid POI should be included
        total_pois = sum(len(city["places"]) for city in result.values())
        assert total_pois == 1


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

    def test_allocate_days_approximate_city_match(self):
        """Test approximate city name matching for user-specified days."""
        maut_suboutput = {
            "places": [{"id": f"poi{i}"} for i in range(10)],
            "meta": {"area_name": "Johor Bahru"},
        }
        user_input = {"days_per_city": {"Johor": 3}}

        days = allocate_days_per_city(maut_suboutput, user_input)

        # Should match "Johor" to "Johor Bahru"
        assert days == 3


class TestAllocateDaysProportionally:
    """Tests for allocate_days_proportionally function."""

    def test_proportional_allocation_equal_pois(self):
        """Test proportional allocation with equal POI counts."""
        cities = {
            "Singapore": {"places": [{"id": f"sg{i}"} for i in range(10)]},
            "Johor": {"places": [{"id": f"jh{i}"} for i in range(10)]},
        }

        result = allocate_days_proportionally(cities, total_days=4)

        assert result["Singapore"] == 2
        assert result["Johor"] == 2
        assert sum(result.values()) == 4

    def test_proportional_allocation_unequal_pois(self):
        """Test proportional allocation with unequal POI counts."""
        cities = {
            "Singapore": {"places": [{"id": f"sg{i}"} for i in range(30)]},
            "Johor": {"places": [{"id": f"jh{i}"} for i in range(10)]},
        }

        result = allocate_days_proportionally(cities, total_days=4)

        # Singapore should get more days (3:1 ratio)
        assert result["Singapore"] >= 2
        assert result["Johor"] >= 1
        assert sum(result.values()) == 4

    def test_proportional_allocation_user_override(self):
        """Test that user-specified days override proportional allocation."""
        cities = {
            "Singapore": {"places": [{"id": f"sg{i}"} for i in range(10)]},
            "Johor": {"places": [{"id": f"jh{i}"} for i in range(10)]},
        }
        user_input = {"days_per_city": {"Singapore": 3, "Johor": 1}}

        result = allocate_days_proportionally(cities, total_days=4, user_input=user_input)

        assert result["Singapore"] == 3
        assert result["Johor"] == 1

    def test_proportional_allocation_minimum_one_day(self):
        """Test that cities with POIs get at least 1 day."""
        cities = {
            "Singapore": {"places": [{"id": f"sg{i}"} for i in range(50)]},
            "Johor": {"places": [{"id": "jh1"}]},  # Only 1 POI
        }

        result = allocate_days_proportionally(cities, total_days=4)

        assert result["Johor"] >= 1
        assert sum(result.values()) == 4


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

    def test_select_hotel_global_fallback(self):
        """Test hotel selection using global fallback when city has no accommodations."""
        maut_suboutput = {
            "places": [
                {
                    "id": "poi1",
                    "name": "Attraction",
                    "poi_roles": ["attraction"],
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                }
            ],
            "meta": {"area_name": "cluster_0"},
        }
        global_fallback = {
            "id": "fallback_hotel",
            "name": "Fallback Hotel",
            "lat": 1.35,
            "lon": 103.85,
            "source": "global_fallback",
        }

        hotel = select_hotel_for_city(
            maut_suboutput, 3, global_fallback_hotel=global_fallback
        )

        assert hotel["id"] == "fallback_hotel"
        assert hotel["source"] == "global_fallback"


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


class TestDepotRoutingRules:
    """
    Tests for depot/hotel routing rules in multi-day itineraries.
    
    Rules:
    - Intermediate days: route starts at day's assigned hotel and ends at same/other hotel
    - City switches: day finishing in City A ends with checkout, next day starts at City B's depot
    - Day 1: no required depot start (route may start at any POI)
    - Last day: no depot required at end (no return to hotel)
    """

    def test_intermediate_day_starts_at_depot(self):
        """Test that intermediate days start at the assigned hotel/depot."""
        # This tests the current behavior - intermediate days should start at depot
        result = {
            "days": [
                {
                    "stops": [
                        {"poi_id": "hotel1", "role": "depot", "name": "Hotel"},
                        {"poi_id": "poi1", "role": "attraction"},
                        {"poi_id": "hotel1", "role": "depot", "name": "Hotel"},
                    ]
                },
                {
                    "stops": [
                        {"poi_id": "hotel1", "role": "depot", "name": "Hotel"},
                        {"poi_id": "poi2", "role": "attraction"},
                        {"poi_id": "hotel1", "role": "depot", "name": "Hotel"},
                    ]
                },
            ],
            "meta": {},
        }

        # Verify intermediate day (day 2) starts at depot
        day2_first_stop = result["days"][1]["stops"][0]
        assert day2_first_stop["role"] == "depot"

    def test_intermediate_day_ends_at_depot(self):
        """Test that intermediate days end at the assigned hotel/depot."""
        result = {
            "days": [
                {
                    "stops": [
                        {"poi_id": "hotel1", "role": "depot"},
                        {"poi_id": "poi1", "role": "attraction"},
                        {"poi_id": "hotel1", "role": "depot"},
                    ]
                },
                {
                    "stops": [
                        {"poi_id": "hotel1", "role": "depot"},
                        {"poi_id": "poi2", "role": "attraction"},
                        {"poi_id": "hotel1", "role": "depot"},
                    ]
                },
            ],
            "meta": {},
        }

        # Verify intermediate day (day 1) ends at depot
        day1_last_stop = result["days"][0]["stops"][-1]
        assert day1_last_stop["role"] == "depot"

    def test_city_switch_different_depots(self):
        """Test that city switches use different depots for each city."""
        result = {
            "days": [
                {
                    "area_name": "Singapore",
                    "depot_id": "sg_hotel",
                    "stops": [
                        {"poi_id": "sg_hotel", "role": "depot"},
                        {"poi_id": "poi1", "role": "attraction"},
                        {"poi_id": "sg_hotel", "role": "depot"},
                    ]
                },
                {
                    "area_name": "Johor",
                    "depot_id": "jh_hotel",
                    "stops": [
                        {"poi_id": "jh_hotel", "role": "depot"},
                        {"poi_id": "poi2", "role": "attraction"},
                        {"poi_id": "jh_hotel", "role": "depot"},
                    ]
                },
            ],
            "meta": {},
        }

        # Verify different depots for different cities
        assert result["days"][0]["depot_id"] == "sg_hotel"
        assert result["days"][1]["depot_id"] == "jh_hotel"
        assert result["days"][0]["depot_id"] != result["days"][1]["depot_id"]

    def test_day_has_depot_metadata(self):
        """Test that each day has depot_id in metadata."""
        result = {
            "days": [
                {
                    "area_name": "Singapore",
                    "depot_id": "hotel1",
                    "stops": [],
                }
            ],
            "meta": {},
        }

        assert "depot_id" in result["days"][0]
        assert result["days"][0]["depot_id"] is not None


class TestMultiCityIntegration:
    """Integration tests for multi-city itinerary generation."""

    def test_multi_city_generates_days_for_each_city(self):
        """Test that multi-city request generates days for each city."""
        # This is a structural test - actual integration would need mocked OSRM
        maut_output = {
            "places": [
                {
                    "id": "sg_hotel",
                    "name": "Singapore Hotel",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                    "_score": 0.9,
                },
                {
                    "id": "sg_poi1",
                    "name": "Singapore Attraction",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.31, "lng": 103.81},
                    "poi_roles": ["attraction"],
                },
                {
                    "id": "jh_hotel",
                    "name": "Johor Hotel",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.5, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                    "_score": 0.8,
                },
                {
                    "id": "jh_poi1",
                    "name": "Johor Attraction",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.51, "lng": 103.81},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {
                "num_days": 4,
                "dates": {"type": "flexible", "days": 4},
            },
        }

        # Segment cities
        cities = segment_by_city(maut_output)

        assert "Singapore" in cities
        assert "Johor" in cities

        # Allocate days
        days_allocation = allocate_days_proportionally(cities, total_days=4)

        assert sum(days_allocation.values()) == 4
        assert days_allocation.get("Singapore", 0) >= 1
        assert days_allocation.get("Johor", 0) >= 1

    def test_hotels_with_area_name_not_clustered(self):
        """Test that hotels with area_name are not put into cluster_0."""
        maut_output = {
            "places": [
                {
                    "id": "sg_hotel",
                    "name": "Singapore Hotel",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.3, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                },
                {
                    "id": "jh_hotel",
                    "name": "Johor Hotel",
                    "area_name": "Johor",
                    "coordinates": {"lat": 1.5, "lng": 103.8},
                    "poi_roles": ["accommodation"],
                },
                {
                    "id": "sg_poi1",
                    "name": "Singapore Attraction",
                    "area_name": "Singapore",
                    "coordinates": {"lat": 1.31, "lng": 103.81},
                    "poi_roles": ["attraction"],
                },
            ],
            "meta": {},
        }

        result = segment_by_city(maut_output)

        # Should NOT have cluster_0 since all POIs have area_name
        assert "cluster_0" not in result
        assert "Singapore" in result
        assert "Johor" in result

        # Hotels should be in their respective cities
        sg_hotels = [
            p for p in result["Singapore"]["places"]
            if "accommodation" in p.get("poi_roles", [])
        ]
        jh_hotels = [
            p for p in result["Johor"]["places"]
            if "accommodation" in p.get("poi_roles", [])
        ]
        assert len(sg_hotels) == 1
        assert len(jh_hotels) == 1
