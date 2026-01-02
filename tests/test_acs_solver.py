"""Tests for ACS-CVRPTW solver - core functionality."""

import pytest
import datetime as dt
from app.services.vrp_model import DaySpec, Node
from app.services.acs_cvrptw import run_acs_cvrptw, _get_base_id


class TestAcsHelpers:
    """Tests for ACS helper functions."""

    def test_get_base_id_strips_day_suffix(self):
        assert _get_base_id("poi123_day0") == "poi123"
        assert _get_base_id("poi123_day5") == "poi123"
        assert _get_base_id("poi123") == "poi123"

    def test_get_base_id_strips_hotel_suffix(self):
        assert _get_base_id("hotel_checkin_day0") == "hotel"
        assert _get_base_id("hotel_checkout_day1") == "hotel"


@pytest.fixture
def simple_setup():
    """Simple node setup for testing."""
    nodes = [
        Node(idx=0, poi_id="hotel1", name="Hotel", role="depot",
             lat=1.3, lon=103.8, service=0, themes=None,
             windows_by_day={0: [(9 * 60, 20 * 60)]}),
        Node(idx=1, poi_id="poi1_day0", name="Attraction 1", role="attraction",
             lat=1.31, lon=103.81, service=60, themes=["cultural_history"],
             windows_by_day={0: [(10 * 60, 18 * 60)]}),
        Node(idx=2, poi_id="meal1_day0", name="Restaurant", role="meal",
             lat=1.32, lon=103.82, service=45, themes=["food"],
             windows_by_day={0: [(11 * 60, 14 * 60)]}),
    ]
    day_specs = [DaySpec(day_index=0, date=dt.date(2025, 1, 15),
                         start_min=9*60, end_min=20*60, depot_id="hotel1")]
    travel = [[0, 10, 10], [10, 0, 10], [10, 10, 0]]
    return nodes, day_specs, travel


class TestAcsSolver:
    """Core tests for ACS solver."""

    def test_returns_days_structure(self, simple_setup):
        """ACS returns days structure."""
        nodes, day_specs, travel = simple_setup
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=1)
        assert "days" in result
        assert len(result["days"]) == 1

    def test_day_has_stops(self, simple_setup):
        """ACS day has stops."""
        nodes, day_specs, travel = simple_setup
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=1)
        assert "stops" in result["days"][0]
        assert len(result["days"][0]["stops"]) >= 1

    def test_respects_time_windows(self, simple_setup):
        """ACS respects day time bounds."""
        nodes, day_specs, travel = simple_setup
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=1)
        for stop in result["days"][0]["stops"]:
            h, m = map(int, stop["arrival"].split(":"))
            arrival_min = h * 60 + m
            assert arrival_min >= day_specs[0].start_min - 1
            assert arrival_min <= day_specs[0].end_min + 1

    def test_handles_mandatory_pois(self):
        """ACS schedules mandatory POIs."""
        nodes = [
            Node(idx=0, poi_id="hotel1", name="Hotel", role="depot",
                 lat=1.3, lon=103.8, service=0, themes=None,
                 windows_by_day={0: [(9*60, 20*60)]}),
            Node(idx=1, poi_id="mand_day0", name="Mandatory", role="attraction",
                 lat=1.31, lon=103.81, service=60, themes=["cultural"],
                 windows_by_day={0: [(10*60, 18*60)]}, is_mandatory=True),
        ]
        day_specs = [DaySpec(day_index=0, date=dt.date(2025, 1, 15),
                             start_min=9*60, end_min=20*60, depot_id="hotel1")]
        travel = [[0, 10], [10, 0]]
        
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=0)
        poi_ids = [s["poi_id"] for s in result["days"][0]["stops"]]
        assert "mand" in poi_ids

    def test_no_duplicate_visits(self):
        """Same POI not visited multiple times across days."""
        nodes = [
            Node(idx=0, poi_id="hotel", name="Hotel", role="depot",
                 lat=1.3, lon=103.8, service=0, themes=None,
                 windows_by_day={0: [(9*60, 20*60)], 1: [(9*60, 20*60)]}),
            Node(idx=1, poi_id="poi1_day0", name="A", role="attraction",
                 lat=1.31, lon=103.81, service=60, themes=["nature"],
                 windows_by_day={0: [(10*60, 18*60)]}),
            Node(idx=2, poi_id="poi1_day1", name="A", role="attraction",
                 lat=1.31, lon=103.81, service=60, themes=["nature"],
                 windows_by_day={1: [(10*60, 18*60)]}),
        ]
        day_specs = [
            DaySpec(day_index=0, date=dt.date(2025, 1, 15), start_min=9*60, end_min=20*60, depot_id="hotel"),
            DaySpec(day_index=1, date=dt.date(2025, 1, 16), start_min=9*60, end_min=20*60, depot_id="hotel"),
        ]
        travel = [[0, 10, 10], [10, 0, 10], [10, 10, 0]]
        
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=0)
        all_ids = []
        for day in result["days"]:
            for stop in day["stops"]:
                if stop.get("role") == "attraction":
                    all_ids.append(_get_base_id(stop["poi_id"]))
        assert len(all_ids) == len(set(all_ids))

    def test_accepts_user_themes(self, simple_setup):
        """ACS accepts user_themes parameter."""
        nodes, day_specs, travel = simple_setup
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel,
                                meals_required=0, user_themes={"cultural_history"})
        assert "days" in result

    def test_hotel_events_in_output(self):
        """Hotel events appear in solver output."""
        from app.services.vrp_model import HotelEvent, HotelEventType
        
        hotel_events = [
            HotelEvent(event_type=HotelEventType.CHECK_IN, hotel_id="h1",
                      hotel_name="Hotel", lat=1.3, lon=103.8,
                      window=(14*60, 16*60), service_time=30),
        ]
        nodes = [
            Node(idx=0, poi_id="h1", name="Hotel", role="depot",
                 lat=1.3, lon=103.8, service=0, themes=None,
                 windows_by_day={0: [(9*60, 20*60)]}),
            Node(idx=1, poi_id="h1_checkin_day0", name="Hotel", role="accommodation",
                 lat=1.3, lon=103.8, service=30, themes=None,
                 windows_by_day={0: [(14*60, 16*60)]}, is_mandatory=True,
                 hotel_event_type="checkin"),
            Node(idx=2, poi_id="poi1_day0", name="A", role="attraction",
                 lat=1.31, lon=103.81, service=60, themes=["nature"],
                 windows_by_day={0: [(10*60, 18*60)]}),
        ]
        day_specs = [DaySpec(day_index=0, date=dt.date(2025, 1, 15),
                             start_min=9*60, end_min=20*60, depot_id="h1",
                             hotel_events=hotel_events)]
        travel = [[0, 0, 10], [0, 0, 10], [10, 10, 0]]
        
        result = run_acs_cvrptw(day_specs=day_specs, nodes=nodes, travel=travel, meals_required=0)
        hotel_stops = [s for s in result["days"][0]["stops"] if s.get("hotel_event_type")]
        assert len(hotel_stops) >= 1
