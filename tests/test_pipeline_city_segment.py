"""Tests for pipeline city segmentation and day allocation.

Segmentation: area_name (primary), KMeans clustering (fallback)
Day allocation: Fixed assignments, proportional allocation, contiguity smoothing
"""

from app.services.pipeline import segment_by_city, select_hotel_for_city, validate_global_rules
from app.services.city_day_allocator import allocate_days_to_cities, extract_fixed_assignments


class TestSegmentByCity:
    """Tests for segment_by_city function."""

    def test_segment_single_city(self):
        """Single city via area_name."""
        maut = {"places": [{"id": "p1", "name": "A", "area_name": "Singapore", "coordinates": {"lat": 1.3, "lng": 103.8}, "roles": ["attraction"]}], "meta": {}}
        result = segment_by_city(maut)
        assert "Singapore" in result
        assert len(result["Singapore"]["places"]) == 1

    def test_segment_multi_city(self):
        """Multiple cities via area_name."""
        maut = {
            "places": [
                {"id": "p1", "area_name": "Johor", "coordinates": {"lat": 1.5, "lng": 103.8}, "roles": ["attraction"]},
                {"id": "p2", "area_name": "Singapore", "coordinates": {"lat": 1.3, "lng": 103.8}, "roles": ["attraction"]},
            ],
            "meta": {},
        }
        result = segment_by_city(maut)
        assert "Johor" in result and "Singapore" in result

    def test_segment_kmeans_fallback(self):
        """KMeans clustering for POIs without area_name."""
        maut = {"places": [{"id": f"p{i}", "coordinates": {"lat": 1.3 + i * 0.01, "lng": 103.8}, "roles": ["attraction"]} for i in range(10)], "meta": {}}
        result = segment_by_city(maut)
        assert any(k.startswith("cluster_") for k in result.keys())


class TestCityDayAllocator:
    """Tests for day allocation."""

    def test_fixed_from_mandatory(self):
        """Fixed assignments from mandatory POIs."""
        fixed = extract_fixed_assignments(total_days=5, mandatory={"poi1": {"day": 1, "poi_destination": "SG"}}, poi_city_lookup={"poi1": "SG"})
        assert 1 in fixed and fixed[1].city == "SG"

    def test_fixed_from_user(self):
        """Fixed assignments from user day_assignments."""
        fixed = extract_fixed_assignments(total_days=5, user_input={"day_assignments": {"1": "Johor"}})
        assert 1 in fixed and fixed[1].city == "Johor"

    def test_allocate_respects_fixed(self):
        """Allocation respects fixed days."""
        cities = {"SG": {"places": [{"id": f"sg{i}"} for i in range(10)]}, "JH": {"places": [{"id": f"jh{i}"} for i in range(10)]}}
        result = allocate_days_to_cities(cities=cities, total_days=5, user_input={"day_assignments": {"1": "JH", "3": "SG"}})
        assert result.day_to_city[1] == "JH" and result.day_to_city[3] == "SG"

    def test_contiguous_blocks(self):
        """Allocation minimizes city switches."""
        cities = {"SG": {"places": [{"id": f"sg{i}"} for i in range(10)]}, "JH": {"places": [{"id": f"jh{i}"} for i in range(10)]}}
        result = allocate_days_to_cities(cities=cities, total_days=6)
        assert len(result.city_switches) <= 1


class TestSelectHotel:
    """Tests for select_hotel_for_city."""

    def test_select_by_score(self):
        """Select hotel by MAUT score."""
        maut = {
            "places": [
                {"id": "h1", "name": "H1", "roles": ["accommodation"], "coordinates": {"lat": 1.3, "lng": 103.8}, "_score": 0.5},
                {"id": "h2", "name": "H2", "roles": ["accommodation"], "coordinates": {"lat": 1.31, "lng": 103.81}, "_score": 0.9},
            ],
            "meta": {"area_name": "SG"},
        }
        hotel = select_hotel_for_city(maut, 3)
        assert hotel["id"] == "h2"
        assert hotel["source"] == "maut"

    def test_user_hotel(self):
        """User-provided hotel takes precedence."""
        hotel = select_hotel_for_city({"places": [], "meta": {"area_name": "SG"}}, 3, {"SG": {"id": "user_h", "lat": 1.3, "lon": 103.8}})
        assert hotel["id"] == "user_h" and hotel["source"] == "user"

    def test_no_accommodation_error(self):
        """Error when no accommodation available."""
        hotel = select_hotel_for_city({"places": [{"id": "p1", "roles": ["attraction"], "coordinates": {"lat": 1.3, "lng": 103.8}}], "meta": {"area_name": "SG"}}, 3)
        assert hotel.get("error") == "no_accommodation"


class TestValidateGlobalRules:
    """Tests for validation."""

    def test_too_many_meals_fails(self):
        """More than 3 meals per day fails."""
        result = {"days": [{"stops": [{"role": "meal"}] * 4}], "meta": {}}
        assert not validate_global_rules(result)["ok"]

    def test_missed_mandatory_fails(self):
        """Missed mandatory POIs fails."""
        result = {"days": [{"stops": []}], "meta": {"missed_mandatory": ["poi1"]}}
        assert not validate_global_rules(result)["ok"]

    def test_valid_passes(self):
        """Valid result passes."""
        result = {"days": [{"stops": [{"role": "attraction"}, {"role": "meal"}]}], "meta": {}}
        assert validate_global_rules(result)["ok"]
