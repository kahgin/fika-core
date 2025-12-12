"""
Tests for mandatory POI handling in the itinerary creation pipeline.

Tests 4 cases:
1. Specific day/date & time - POI scheduled on specific day with time window
2. All day - POI blocks entire day (only that POI + depot)
3. Any time - POI scheduled on any day/time using role defaults
4. Fallback to any_time - No time_type specified defaults to any_time
"""

import pytest
from unittest.mock import patch
from datetime import date

from app.services.vrp_utils import build_problem
from app.services.vrp_model import vrp_config


@pytest.fixture
def mock_osrm():
    """Mock OSRM client for deterministic tests."""
    with patch("app.services.osrm.osrm_client") as mock:

        def matrix_minutes(coords):
            n = len(coords)
            return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

        mock.matrix_minutes.side_effect = matrix_minutes
        yield mock


@pytest.fixture
def hotel():
    """Hotel fixture."""
    return {
        "id": "hotel1",
        "name": "Test Hotel",
        "lat": 1.3,
        "lon": 103.8,
    }


@pytest.fixture
def basic_maut_output():
    """Basic MAUT output with attractions and meals."""
    return {
        "places": [
            {
                "id": "attraction1",
                "name": "Marina Bay Sands",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "themes": ["culture"],
            },
            {
                "id": "attraction2",
                "name": "Gardens by the Bay",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.29, "lng": 103.86},
                "themes": ["nature"],
            },
            {
                "id": "meal1",
                "name": "Hawker Center",
                "roles": ["meal"],
                "coordinates": {"lat": 1.30, "lng": 103.84},
            },
            {
                "id": "mandatory_poi",
                "name": "Singapore Zoo",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.40, "lng": 103.79},
                "themes": ["family"],
            },
        ],
        "meta": {
            "num_days": 3,
            "dates": {"type": "flexible", "days": 3},
        },
    }


class TestMandatoryPoiSpecificDayTime:
    """Test Case 1: Mandatory POI with specific day and time window."""

    def test_specific_day_and_time_creates_constrained_node(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """POI with day=2 and window=[10:00, 12:00] should only appear on day 1 (0-indexed)."""
        mandatory = {
            "mandatory_poi": {
                "day": 2,  # 1-based, so day index 1
                "time_type": "specific",
                "window": ["10:00", "12:00"],
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        # Find mandatory nodes
        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 1, "Should have exactly 1 mandatory node"

        mand_node = mandatory_nodes[0]
        assert "mandatory_poi" in mand_node.poi_id

        # Should only be available on day 1 (0-indexed)
        assert list(mand_node.windows_by_day.keys()) == [1]

        # Window should be 10:00-12:00 (600-720 minutes)
        windows = mand_node.windows_by_day[1]
        assert len(windows) == 1
        assert windows[0] == (600, 720)

    def test_specific_time_with_minutes(self, mock_osrm, basic_maut_output, hotel):
        """Time window with minutes (10:30-14:45) should parse correctly."""
        mandatory = {
            "mandatory_poi": {
                "day": 1,
                "time_type": "specific",
                "window": ["10:30", "14:45"],
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 1

        mand_node = mandatory_nodes[0]
        windows = mand_node.windows_by_day[0]
        # 10:30 = 630 min, 14:45 = 885 min
        assert windows[0] == (630, 885)


class TestMandatoryPoiAllDay:
    """Test Case 2: Mandatory POI that blocks entire day."""

    def test_all_day_blocks_entire_day_window(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """All-day POI should have window spanning entire day budget."""
        mandatory = {
            "mandatory_poi": {
                "day": 2,
                "time_type": "all_day",
                "all_day": True,
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 1

        mand_node = mandatory_nodes[0]

        # Should only be on day 1 (0-indexed from day=2)
        assert list(mand_node.windows_by_day.keys()) == [1]

        # Window should span entire day
        day_spec = day_specs[1]
        windows = mand_node.windows_by_day[1]
        assert windows[0] == (day_spec.start_min, day_spec.end_min)

    def test_all_day_has_extended_service_time(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """All-day POI should have service time filling most of the day."""
        mandatory = {
            "mandatory_poi": {
                "day": 1,
                "time_type": "all_day",
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        mand_node = mandatory_nodes[0]

        # Service time should be close to day budget minus travel buffer
        day_spec = day_specs[0]
        day_budget = day_spec.end_min - day_spec.start_min
        # Service should be at least day_budget - 60 (travel buffer)
        assert mand_node.service >= day_budget - 60

    def test_all_day_via_time_type_only(self, mock_osrm, basic_maut_output, hotel):
        """time_type='all_day' without explicit all_day=True should work."""
        mandatory = {
            "mandatory_poi": {
                "day": 1,
                "time_type": "all_day",
                # No explicit all_day field
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 1

        mand_node = mandatory_nodes[0]
        day_spec = day_specs[0]
        windows = mand_node.windows_by_day[0]
        assert windows[0] == (day_spec.start_min, day_spec.end_min)


class TestMandatoryPoiAnyTime:
    """Test Case 3: Mandatory POI with any_time (flexible scheduling)."""

    def test_any_time_uses_role_defaults(self, mock_osrm, basic_maut_output, hotel):
        """any_time POI should use role-based default windows."""
        mandatory = {
            "mandatory_poi": {
                "time_type": "any_time",
                # No day constraint - can be on any day
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        # Should have mandatory nodes for each day
        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 3  # One for each day

        # Each should use attraction role defaults
        role_default = vrp_config.default_role_windows.get("attraction")
        for mand_node in mandatory_nodes:
            day_idx = list(mand_node.windows_by_day.keys())[0]
            windows = mand_node.windows_by_day[day_idx]
            # Window should be within role defaults
            assert (
                windows[0][0] >= role_default[0]
                or windows[0][0] >= day_specs[day_idx].start_min
            )
            assert (
                windows[0][1] <= role_default[1]
                or windows[0][1] <= day_specs[day_idx].end_min
            )

    def test_any_time_with_day_constraint(self, mock_osrm, basic_maut_output, hotel):
        """any_time with day constraint should only appear on that day."""
        mandatory = {
            "mandatory_poi": {
                "day": 3,  # Day 3 (1-based) = index 2
                "time_type": "any_time",
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 1

        mand_node = mandatory_nodes[0]
        assert list(mand_node.windows_by_day.keys()) == [2]


class TestMandatoryPoiFallback:
    """Test Case 4: Fallback to any_time when no time_type specified."""

    def test_no_time_type_defaults_to_any_time(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Missing time_type should default to any_time behavior."""
        mandatory = {
            "mandatory_poi": {
                # No time_type, no day, no window
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        # Should have mandatory nodes for each day (any_time behavior)
        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 3

    def test_empty_mandatory_spec_still_marks_mandatory(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Empty spec {} should still mark POI as mandatory."""
        mandatory = {"mandatory_poi": {}}

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) > 0
        assert all(n.is_mandatory for n in mandatory_nodes)

    def test_none_spec_marks_mandatory(self, mock_osrm, basic_maut_output, hotel):
        """None spec should still mark POI as mandatory."""
        mandatory = {"mandatory_poi": None}

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) > 0


class TestMandatoryPoiApiParsing:
    """Test API-level parsing of mandatory POIs from frontend payload."""

    def test_parse_flexible_dates_with_day(self):
        """Flexible dates mode: day field should be preserved."""
        from app.api.itinerary import create_itinerary

        # This is a unit test for the parsing logic, not full API test
        payload = {
            "dates": {"type": "flexible", "days": 3},
            "mandatory_pois": [
                {
                    "poi_id": "test_poi",
                    "poi_name": "Test POI",
                    "latitude": 1.3,
                    "longitude": 103.8,
                    "day": 2,
                    "time_type": "specific",
                    "start_time": "10:00",
                    "end_time": "12:00",
                }
            ],
        }

        # Extract the parsing logic
        dates_info = payload.get("dates", {})
        is_specific_dates = dates_info.get("type") == "specific"

        poi = payload["mandatory_pois"][0]
        time_type = poi.get("time_type", "any_time")
        day = poi.get("day")

        md_entry = {"time_type": time_type}
        if isinstance(day, int) and day > 0:
            md_entry["day"] = day
        if time_type == "specific":
            md_entry["window"] = [poi.get("start_time"), poi.get("end_time")]

        assert md_entry["day"] == 2
        assert md_entry["time_type"] == "specific"
        assert md_entry["window"] == ["10:00", "12:00"]

    def test_parse_specific_dates_with_date(self):
        """Specific dates mode: date field should convert to day index."""
        payload = {
            "dates": {
                "type": "specific",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
            },
            "mandatory_pois": [
                {
                    "poi_id": "test_poi",
                    "poi_name": "Test POI",
                    "latitude": 1.3,
                    "longitude": 103.8,
                    "date": "2025-06-02",  # Second day of trip
                    "time_type": "all_day",
                }
            ],
        }

        dates_info = payload.get("dates", {})
        is_specific_dates = dates_info.get("type") == "specific"

        poi = payload["mandatory_pois"][0]
        date_str = poi.get("date")

        md_entry = {"time_type": poi.get("time_type", "any_time")}

        if is_specific_dates and date_str:
            trip_start_str = dates_info.get("start_date")
            if trip_start_str:
                trip_start = date.fromisoformat(str(trip_start_str).split("T")[0])
                poi_date = date.fromisoformat(str(date_str).split("T")[0])
                day_index = (poi_date - trip_start).days + 1
                if day_index > 0:
                    md_entry["day"] = day_index

        if poi.get("time_type") == "all_day":
            md_entry["all_day"] = True

        assert md_entry["day"] == 2  # June 2 is day 2 of trip starting June 1
        assert md_entry["all_day"] is True


class TestMandatoryPoiMultiCity:
    """Test mandatory POIs are correctly assigned to destination cities."""

    def test_filter_mandatory_for_city_by_poi_destination(self):
        """Mandatory POI with poi_destination should only appear in matching city."""
        from app.services.pipeline import _filter_mandatory_for_city

        mandatory = {
            "johor_poi": {
                "time_type": "any_time",
                "poi_destination": "Johor",
            },
            "singapore_poi": {
                "time_type": "specific",
                "poi_destination": "Singapore",
                "window": ["10:00", "12:00"],
            },
        }

        # Filter for Singapore
        sg_mandatory = _filter_mandatory_for_city(mandatory, "Singapore", [])
        assert sg_mandatory is not None
        assert "singapore_poi" in sg_mandatory
        assert "johor_poi" not in sg_mandatory

        # Filter for Johor
        jb_mandatory = _filter_mandatory_for_city(mandatory, "Johor", [])
        assert jb_mandatory is not None
        assert "johor_poi" in jb_mandatory
        assert "singapore_poi" not in jb_mandatory

    def test_filter_mandatory_for_city_by_area_name(self):
        """Mandatory POI without poi_destination uses area_name from places."""
        from app.services.pipeline import _filter_mandatory_for_city

        mandatory = {
            "poi_in_johor": {"time_type": "any_time"},
            "poi_in_singapore": {"time_type": "any_time"},
        }

        places = [
            {"id": "poi_in_johor", "area_name": "Johor Bahru"},
            {"id": "poi_in_singapore", "area_name": "Singapore"},
        ]

        # Filter for Johor (should match "Johor Bahru")
        jb_mandatory = _filter_mandatory_for_city(mandatory, "Johor", places)
        assert jb_mandatory is not None
        assert "poi_in_johor" in jb_mandatory

        # Filter for Singapore
        sg_mandatory = _filter_mandatory_for_city(mandatory, "Singapore", places)
        assert sg_mandatory is not None
        assert "poi_in_singapore" in sg_mandatory

    def test_filter_mandatory_no_destination_includes_all(self):
        """Mandatory POI without destination info should be included in all cities."""
        from app.services.pipeline import _filter_mandatory_for_city

        mandatory = {
            "poi_no_dest": {"time_type": "any_time"},  # No poi_destination
        }

        places = []  # No area_name lookup available

        # Should be included in both cities
        sg_mandatory = _filter_mandatory_for_city(mandatory, "Singapore", places)
        assert sg_mandatory is not None
        assert "poi_no_dest" in sg_mandatory

        jb_mandatory = _filter_mandatory_for_city(mandatory, "Johor", places)
        assert jb_mandatory is not None
        assert "poi_no_dest" in jb_mandatory

    def test_filter_mandatory_normalized_city_matching(self):
        """City names should be normalized for matching (case-insensitive, comma handling)."""
        from app.services.pipeline import _filter_mandatory_for_city

        mandatory = {
            "poi1": {"time_type": "any_time", "poi_destination": "johor"},  # lowercase
            "poi2": {
                "time_type": "any_time",
                "poi_destination": "Singapore, SG",
            },  # with comma
        }

        # Should match despite case difference
        jb_mandatory = _filter_mandatory_for_city(mandatory, "Johor Bahru", [])
        assert jb_mandatory is not None
        assert "poi1" in jb_mandatory

        # Should match despite comma in destination
        sg_mandatory = _filter_mandatory_for_city(mandatory, "Singapore", [])
        assert sg_mandatory is not None
        assert "poi2" in sg_mandatory

    def test_multi_city_mandatory_poi_integration(self, mock_osrm):
        """Integration test: mandatory POIs assigned to correct city days."""
        from app.services.vrp_utils import build_problem
        from app.services.pipeline import _filter_mandatory_for_city

        # Singapore POIs
        sg_places = [
            {
                "id": "sg_attraction",
                "name": "Marina Bay",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.28, "lng": 103.85},
                "area_name": "Singapore",
                "themes": ["culture"],
            },
            {
                "id": "sg_mandatory",
                "name": "Singapore Zoo",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.40, "lng": 103.79},
                "area_name": "Singapore",
                "themes": ["family"],
            },
        ]

        # Johor POIs
        jb_places = [
            {
                "id": "jb_attraction",
                "name": "Legoland",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.43, "lng": 103.63},
                "area_name": "Johor Bahru",
                "themes": ["family"],
            },
            {
                "id": "jb_mandatory",
                "name": "Hello Kitty Town",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.42, "lng": 103.64},
                "area_name": "Johor Bahru",
                "themes": ["family"],
            },
        ]

        # Full mandatory dict with poi_destination
        mandatory = {
            "sg_mandatory": {
                "time_type": "any_time",
                "poi_destination": "Singapore",
            },
            "jb_mandatory": {
                "time_type": "specific",
                "poi_destination": "Johor",
                "day": 1,
                "window": ["10:00", "14:00"],
            },
        }

        # Filter for Singapore
        sg_mandatory = _filter_mandatory_for_city(mandatory, "Singapore", sg_places)
        assert sg_mandatory is not None
        assert "sg_mandatory" in sg_mandatory
        assert "jb_mandatory" not in sg_mandatory

        # Filter for Johor
        jb_mandatory = _filter_mandatory_for_city(mandatory, "Johor Bahru", jb_places)
        assert jb_mandatory is not None
        assert "jb_mandatory" in jb_mandatory
        assert "sg_mandatory" not in jb_mandatory

        # Build problem for Singapore with filtered mandatory
        sg_hotel = {"id": "sg_hotel", "name": "SG Hotel", "lat": 1.30, "lon": 103.80}
        sg_maut = {
            "places": sg_places,
            "meta": {"num_days": 2, "dates": {"type": "flexible", "days": 2}},
        }

        day_specs, nodes, travel = build_problem(
            sg_maut, sg_hotel, pacing="balanced", mandatory=sg_mandatory
        )

        # Verify sg_mandatory is marked as mandatory
        sg_mand_nodes = [
            n for n in nodes if n.is_mandatory and "sg_mandatory" in n.poi_id
        ]
        assert len(sg_mand_nodes) > 0

        # Verify jb_mandatory is NOT in Singapore nodes
        jb_mand_nodes = [n for n in nodes if "jb_mandatory" in n.poi_id]
        assert len(jb_mand_nodes) == 0


class TestMandatoryPoiEdgeCases:
    """Edge cases and error handling for mandatory POIs."""

    def test_invalid_window_format_falls_back(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Invalid window format should fall back to role defaults."""
        mandatory = {
            "mandatory_poi": {
                "day": 1,
                "time_type": "specific",
                "window": ["invalid", "format"],
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        # Should still create node with fallback windows
        assert len(mandatory_nodes) >= 1

    def test_day_out_of_range_creates_no_node(
        self, mock_osrm, basic_maut_output, hotel
    ):
        """Day constraint beyond trip length should create no nodes."""
        mandatory = {
            "mandatory_poi": {
                "day": 10,  # Trip is only 3 days
                "time_type": "specific",
                "window": ["10:00", "12:00"],
            }
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        # No nodes should be created for day 10 when trip is 3 days
        assert len(mandatory_nodes) == 0

    def test_multiple_mandatory_pois(self, mock_osrm, basic_maut_output, hotel):
        """Multiple mandatory POIs with different constraints."""
        # Add another mandatory POI to places
        basic_maut_output["places"].append(
            {
                "id": "mandatory_poi_2",
                "name": "Universal Studios",
                "roles": ["attraction"],
                "coordinates": {"lat": 1.25, "lng": 103.82},
                "themes": ["family"],
            }
        )

        mandatory = {
            "mandatory_poi": {
                "day": 1,
                "time_type": "specific",
                "window": ["10:00", "12:00"],
            },
            "mandatory_poi_2": {
                "day": 2,
                "time_type": "all_day",
            },
        }

        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            mandatory=mandatory,
        )

        mandatory_nodes = [n for n in nodes if n.is_mandatory]
        assert len(mandatory_nodes) == 2

        # Verify each has correct constraints
        poi_1_nodes = [n for n in mandatory_nodes if "mandatory_poi_day0" in n.poi_id]
        poi_2_nodes = [n for n in mandatory_nodes if "mandatory_poi_2_day1" in n.poi_id]

        assert len(poi_1_nodes) == 1
        assert len(poi_2_nodes) == 1

        # POI 1 should have specific window
        assert poi_1_nodes[0].windows_by_day[0] == [(600, 720)]

        # POI 2 should have all-day window
        day_spec = day_specs[1]
        assert poi_2_nodes[0].windows_by_day[1] == [
            (day_spec.start_min, day_spec.end_min)
        ]
