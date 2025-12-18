"""
Tests for hotel event handling in the VRP model.

RULES:
1. Single-day single-city trip: NO hotel events (no overnight stay)
2. Each hotel has exactly ONE check-in and ONE check-out (paired events)
3. Check-in always happens on FIRST day of a city segment
4. Check-out always happens on FIRST day of NEXT city (transition day) OR last day if last city
5. Single-day LAST destination: Only check-out from PREVIOUS hotel (no check-in to current)
6. STAY events for intermediate days (between check-in and check-out)
7. num_checkin == num_checkout (globally)
8. hotel[i].checkin always before hotel[i].checkout in time
9. Hotel event nodes include latitude/longitude

TRANSITION DAY HANDLING:
- When moving to a new city, check-out from previous hotel happens on day 0 of new city
- This is handled by the NEW city segment, not the previous one
"""

import pytest
import datetime as dt
from unittest.mock import patch

from app.services.vrp_model import (
    DaySpec,
    HotelEvent,
    HotelEventType,
    vrp_config,
)
from app.services.vrp_utils import (
    determine_hotel_events,
    create_hotel_event_nodes,
    build_problem,
)


class TestHotelEventType:
    """Tests for HotelEventType enum."""

    def test_hotel_event_types_exist(self):
        """Test all hotel event types are defined."""
        assert HotelEventType.CHECK_IN.value == "check_in"
        assert HotelEventType.CHECK_OUT.value == "check_out"
        assert HotelEventType.STAY.value == "stay"


class TestHotelEvent:
    """Tests for HotelEvent dataclass."""

    def test_hotel_event_creation(self):
        """Test HotelEvent can be created with all required fields."""
        event = HotelEvent(
            event_type=HotelEventType.CHECK_IN,
            hotel_id="hotel1",
            hotel_name="Test Hotel",
            lat=1.3,
            lon=103.8,
            window=(14 * 60, 16 * 60),
            service_time=30,
        )

        assert event.event_type == HotelEventType.CHECK_IN
        assert event.hotel_id == "hotel1"
        assert event.hotel_name == "Test Hotel"
        assert event.lat == 1.3
        assert event.lon == 103.8
        assert event.window == (14 * 60, 16 * 60)
        assert event.service_time == 30


class TestDetermineHotelEvents:
    """Tests for determine_hotel_events function."""

    @pytest.fixture
    def hotel(self):
        return {
            "id": "hotel1",
            "name": "Test Hotel",
            "lat": 1.3,
            "lon": 103.8,
        }

    @pytest.fixture
    def prev_hotel(self):
        return {
            "id": "prev_hotel",
            "name": "Previous Hotel",
            "lat": 1.2,
            "lon": 103.7,
        }

    def test_single_day_single_destination_no_hotel_events(self, hotel):
        """Rule 1: Single-day trip with single destination has NO hotel events."""
        events = determine_hotel_events(
            num_days=1,
            hotel=hotel,
            is_first_city=True,
            is_last_city=True,
        )

        assert len(events) == 0

    def test_single_day_first_city_not_last_has_hotel_events(self, hotel):
        """Single-day first city that is NOT last should have check-in."""
        events = determine_hotel_events(
            num_days=1,
            hotel=hotel,
            is_first_city=True,
            is_last_city=False,
        )

        # Should have check-in on day 0 (checkout will be on next city's day 0)
        assert 0 in events
        day0_types = [e.event_type for e in events[0]]
        assert HotelEventType.CHECK_IN in day0_types
        # No checkout - will be handled by next city's transition day
        assert HotelEventType.CHECK_OUT not in day0_types

    def test_multi_day_first_city_not_last(self, hotel):
        """Multi-day first city (not last) has check-in on day 0, STAY on intermediate days."""
        events = determine_hotel_events(
            num_days=2,
            hotel=hotel,
            is_first_city=True,
            is_last_city=False,
        )

        # Day 0 should have check-in only
        assert 0 in events
        day0_types = [e.event_type for e in events[0]]
        assert HotelEventType.CHECK_IN in day0_types
        assert HotelEventType.CHECK_OUT not in day0_types

        # Day 1 should have STAY (checkout handled by next city)
        assert 1 in events
        day1_types = [e.event_type for e in events[1]]
        assert HotelEventType.STAY in day1_types

    def test_multi_day_first_city_is_last(self, hotel):
        """Multi-day first city that IS last has check-in on day 0, STAY on middle days, check-out on last day."""
        events = determine_hotel_events(
            num_days=3,
            hotel=hotel,
            is_first_city=True,
            is_last_city=True,
        )

        # Day 0 should have check-in only
        assert 0 in events
        day0_types = [e.event_type for e in events[0]]
        assert HotelEventType.CHECK_IN in day0_types
        assert HotelEventType.CHECK_OUT not in day0_types

        # Day 1 (middle day) should have STAY
        assert 1 in events
        day1_types = [e.event_type for e in events[1]]
        assert HotelEventType.STAY in day1_types

        # Day 2 (last day) should have check-out only
        assert 2 in events
        day2_types = [e.event_type for e in events[2]]
        assert HotelEventType.CHECK_OUT in day2_types
        assert HotelEventType.CHECK_IN not in day2_types

    def test_single_day_last_city_only_checkout_from_prev(self, hotel, prev_hotel):
        """Rule 5: Single-day LAST city only has checkout from PREVIOUS hotel."""
        events = determine_hotel_events(
            num_days=1,
            hotel=hotel,
            is_first_city=False,
            is_last_city=True,
            prev_city_hotel=prev_hotel,
        )

        # Day 0 should have checkout from previous hotel only
        assert 0 in events
        assert len(events[0]) == 1
        event = events[0][0]
        assert event.event_type == HotelEventType.CHECK_OUT
        assert event.hotel_id == prev_hotel["id"]

    def test_multi_day_non_first_city_transition_day(self, hotel, prev_hotel):
        """Non-first city has checkout from prev on day 0 + check-in to current."""
        events = determine_hotel_events(
            num_days=2,
            hotel=hotel,
            is_first_city=False,
            is_last_city=True,
            prev_city_hotel=prev_hotel,
        )

        # Day 0 should have checkout from prev + checkin to current
        assert 0 in events
        day0_types = [e.event_type for e in events[0]]
        assert HotelEventType.CHECK_OUT in day0_types
        assert HotelEventType.CHECK_IN in day0_types

        # Verify the hotels are correct
        checkout_event = next(e for e in events[0] if e.event_type == HotelEventType.CHECK_OUT)
        checkin_event = next(e for e in events[0] if e.event_type == HotelEventType.CHECK_IN)
        assert checkout_event.hotel_id == prev_hotel["id"]
        assert checkin_event.hotel_id == hotel["id"]

        # Day 1 (last day) should have checkout from current hotel
        assert 1 in events
        day1_types = [e.event_type for e in events[1]]
        assert HotelEventType.CHECK_OUT in day1_types
        checkout_last = next(e for e in events[1] if e.event_type == HotelEventType.CHECK_OUT)
        assert checkout_last.hotel_id == hotel["id"]

    def test_each_hotel_has_both_checkin_and_checkout(self, hotel):
        """Rule 2: Each hotel must have both check-in and check-out (paired events)."""
        events = determine_hotel_events(
            num_days=5,
            hotel=hotel,
            is_first_city=True,
            is_last_city=True,
        )

        # Collect all events for this hotel
        hotel_events = []
        for day_events in events.values():
            for event in day_events:
                if event.hotel_id == hotel["id"]:
                    hotel_events.append(event)

        # Should have exactly one check-in and one check-out
        checkins = [e for e in hotel_events if e.event_type == HotelEventType.CHECK_IN]
        checkouts = [e for e in hotel_events if e.event_type == HotelEventType.CHECK_OUT]

        assert len(checkins) == 1, "Hotel should have exactly one check-in"
        assert len(checkouts) == 1, "Hotel should have exactly one check-out"

    def test_checkin_checkout_count_equal(self, hotel):
        """Rule 7: num_checkin == num_checkout."""
        events = determine_hotel_events(
            num_days=5,
            hotel=hotel,
            is_first_city=True,
            is_last_city=True,
        )

        all_events = []
        for day_events in events.values():
            all_events.extend(day_events)

        checkins = [e for e in all_events if e.event_type == HotelEventType.CHECK_IN]
        checkouts = [e for e in all_events if e.event_type == HotelEventType.CHECK_OUT]

        assert len(checkins) == len(checkouts)

    def test_stay_events_on_intermediate_days(self, hotel):
        """Rule 6: STAY events on intermediate days."""
        events = determine_hotel_events(
            num_days=5,
            hotel=hotel,
            is_first_city=True,
            is_last_city=True,
        )

        # Days 1, 2, 3 should have STAY events
        for day_idx in [1, 2, 3]:
            assert day_idx in events
            day_types = [e.event_type for e in events[day_idx]]
            assert HotelEventType.STAY in day_types


class TestDaySpecHotelEvents:
    """Tests for DaySpec hotel event properties."""

    def test_day_spec_has_hotel_event(self):
        """Test has_hotel_event property."""
        day_with_event = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
            hotel_events=[
                HotelEvent(
                    event_type=HotelEventType.CHECK_IN,
                    hotel_id="hotel1",
                    hotel_name="Test Hotel",
                    lat=1.3,
                    lon=103.8,
                    window=(14 * 60, 16 * 60),
                )
            ],
        )

        day_without_event = DaySpec(
            day_index=1,
            date=dt.date(2025, 1, 16),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
            hotel_events=[],
        )

        assert day_with_event.has_hotel_event is True
        assert day_without_event.has_hotel_event is False

    def test_day_spec_has_check_in(self):
        """Test has_check_in property."""
        day_with_checkin = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
            hotel_events=[
                HotelEvent(
                    event_type=HotelEventType.CHECK_IN,
                    hotel_id="hotel1",
                    hotel_name="Test Hotel",
                    lat=1.3,
                    lon=103.8,
                    window=(14 * 60, 16 * 60),
                )
            ],
        )

        assert day_with_checkin.has_check_in is True
        assert day_with_checkin.has_check_out is False

    def test_day_spec_is_transition_day(self):
        """Test is_transition_day property - has both checkout and checkin."""
        transition_day = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="hotel1",
            hotel_events=[
                HotelEvent(
                    event_type=HotelEventType.CHECK_OUT,
                    hotel_id="prev_hotel",
                    hotel_name="Previous Hotel",
                    lat=1.2,
                    lon=103.7,
                    window=(10 * 60, 12 * 60),
                ),
                HotelEvent(
                    event_type=HotelEventType.CHECK_IN,
                    hotel_id="hotel1",
                    hotel_name="Test Hotel",
                    lat=1.3,
                    lon=103.8,
                    window=(14 * 60, 16 * 60),
                ),
            ],
        )

        assert transition_day.is_transition_day is True


class TestCreateHotelEventNodes:
    """Tests for create_hotel_event_nodes function."""

    def test_creates_nodes_with_lat_lon(self):
        """Rule 9: Hotel event nodes include latitude and longitude."""
        day_specs = [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
                hotel_events=[
                    HotelEvent(
                        event_type=HotelEventType.CHECK_IN,
                        hotel_id="hotel1",
                        hotel_name="Test Hotel",
                        lat=1.3,
                        lon=103.8,
                        window=(14 * 60, 16 * 60),
                        service_time=30,
                    )
                ],
            ),
        ]

        nodes, next_idx = create_hotel_event_nodes(day_specs, start_idx=1)

        assert len(nodes) == 1
        node = nodes[0]
        assert node.lat == 1.3
        assert node.lon == 103.8
        assert node.is_mandatory is True
        assert node.role == "accommodation"

    def test_creates_nodes_for_transition_day(self):
        """Test both checkout and checkin nodes are created for transition day."""
        day_specs = [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
                hotel_events=[
                    HotelEvent(
                        event_type=HotelEventType.CHECK_OUT,
                        hotel_id="prev_hotel",
                        hotel_name="Previous Hotel",
                        lat=1.2,
                        lon=103.7,
                        window=(10 * 60, 12 * 60),
                        service_time=30,
                    ),
                    HotelEvent(
                        event_type=HotelEventType.CHECK_IN,
                        hotel_id="hotel1",
                        hotel_name="Test Hotel",
                        lat=1.3,
                        lon=103.8,
                        window=(14 * 60, 16 * 60),
                        service_time=30,
                    ),
                ],
            ),
        ]

        nodes, next_idx = create_hotel_event_nodes(day_specs, start_idx=1)

        assert len(nodes) == 2
        assert next_idx == 3

        # Check checkout node
        checkout_node = next(n for n in nodes if "checkout" in n.poi_id)
        assert checkout_node.is_mandatory is True
        assert checkout_node.lat == 1.2
        assert checkout_node.lon == 103.7

        # Check checkin node
        checkin_node = next(n for n in nodes if "checkin" in n.poi_id)
        assert checkin_node.is_mandatory is True
        assert checkin_node.lat == 1.3
        assert checkin_node.lon == 103.8

    def test_stay_nodes_not_mandatory(self):
        """STAY nodes should not be mandatory for solver routing."""
        day_specs = [
            DaySpec(
                day_index=1,
                date=dt.date(2025, 1, 16),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="hotel1",
                hotel_events=[
                    HotelEvent(
                        event_type=HotelEventType.STAY,
                        hotel_id="hotel1",
                        hotel_name="Test Hotel",
                        lat=1.3,
                        lon=103.8,
                        window=(0, 24 * 60),
                        service_time=0,
                    )
                ],
            ),
        ]

        nodes, next_idx = create_hotel_event_nodes(day_specs, start_idx=1)

        assert len(nodes) == 1
        stay_node = nodes[0]
        assert stay_node.is_mandatory is False
        assert "stay" in stay_node.poi_id


class TestBuildProblemHotelEvents:
    """Tests for build_problem with hotel event handling."""

    @pytest.fixture
    def mock_osrm(self):
        """Mock OSRM client for deterministic tests."""
        with patch("app.services.osrm.osrm_client") as mock:

            def matrix_minutes(coords):
                n = len(coords)
                return [[10 if i != j else 0 for j in range(n)] for i in range(n)]

            mock.matrix_minutes.side_effect = matrix_minutes
            yield mock

    @pytest.fixture
    def basic_maut_output(self):
        return {
            "places": [
                {
                    "id": "attraction1",
                    "name": "Marina Bay",
                    "roles": ["attraction"],
                    "coordinates": {"lat": 1.28, "lng": 103.85},
                    "themes": ["cultural_history"],
                },
            ],
            "meta": {
                "num_days": 3,
                "dates": {"type": "flexible", "days": 3},
            },
        }

    @pytest.fixture
    def single_day_maut_output(self):
        return {
            "places": [
                {
                    "id": "attraction1",
                    "name": "Marina Bay",
                    "roles": ["attraction"],
                    "coordinates": {"lat": 1.28, "lng": 103.85},
                    "themes": ["cultural_history"],
                },
            ],
            "meta": {
                "num_days": 1,
                "dates": {"type": "flexible", "days": 1},
            },
        }

    @pytest.fixture
    def hotel(self):
        return {
            "id": "hotel1",
            "name": "Test Hotel",
            "lat": 1.3,
            "lon": 103.8,
        }

    def test_single_day_trip_no_hotel_events(self, mock_osrm, single_day_maut_output, hotel):
        """Rule 1: Single-day trip should have NO hotel event nodes."""
        day_specs, nodes, travel = build_problem(
            single_day_maut_output,
            hotel,
            pacing="balanced",
            is_first_city=True,
            is_last_city=True,
        )

        assert len(day_specs) == 1
        assert day_specs[0].has_hotel_event is False

        # No accommodation nodes (except depot)
        accommodation_nodes = [n for n in nodes if n.role == "accommodation" and n.is_mandatory]
        assert len(accommodation_nodes) == 0

    def test_multi_day_trip_has_hotel_events(self, mock_osrm, basic_maut_output, hotel):
        """Multi-day trip should have check-in, STAY, and check-out nodes."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            is_first_city=True,
            is_last_city=True,
        )

        # First day should have check-in
        assert day_specs[0].has_check_in is True

        # Last day should have check-out
        assert day_specs[-1].has_check_out is True

        # Middle day should have STAY event
        assert day_specs[1].has_hotel_event is True
        stay_events = [e for e in day_specs[1].hotel_events if e.event_type == HotelEventType.STAY]
        assert len(stay_events) == 1

        # Should have 2 mandatory hotel event nodes (1 check-in, 1 check-out)
        # STAY nodes are not mandatory
        mandatory_hotel_nodes = [n for n in nodes if n.role == "accommodation" and n.is_mandatory]
        assert len(mandatory_hotel_nodes) == 2

    def test_hotel_event_nodes_have_lat_lon(self, mock_osrm, basic_maut_output, hotel):
        """Hotel event nodes should include latitude and longitude."""
        day_specs, nodes, travel = build_problem(
            basic_maut_output,
            hotel,
            pacing="balanced",
            is_first_city=True,
            is_last_city=True,
        )

        hotel_event_nodes = [n for n in nodes if n.role == "accommodation" and n.is_mandatory]

        for node in hotel_event_nodes:
            assert node.lat is not None
            assert node.lon is not None
            assert node.lat == hotel["lat"]
            assert node.lon == hotel["lon"]


class TestVRPConfigHotelWindows:
    """Tests for hotel time window configuration."""

    def test_check_in_window_defined(self):
        """Test check-in window is defined in config."""
        assert hasattr(vrp_config, "hotel_check_in_window")
        start, end = vrp_config.hotel_check_in_window
        assert start == 14 * 60  # 14:00
        assert end == 16 * 60  # 16:00

    def test_check_out_window_defined(self):
        """Test check-out window is defined in config."""
        assert hasattr(vrp_config, "hotel_check_out_window")
        start, end = vrp_config.hotel_check_out_window
        assert start == 10 * 60  # 10:00
        assert end == 12 * 60  # 12:00

    def test_hotel_service_time_defined(self):
        """Test hotel service time is defined in config."""
        assert hasattr(vrp_config, "hotel_service_time")
        assert vrp_config.hotel_service_time == 30  # 30 minutes


class TestMultiCityHotelEvents:
    """Tests for multi-city hotel event scenarios.

    Example: 2-day Singapore -> 1-day Johor

    Expected:
    - Singapore Day 1: Check-in to Singapore hotel
    - Singapore Day 2: STAY at Singapore hotel (checkout handled by Johor)
    - Johor Day 1: Check-out from Singapore hotel (no check-in - single day last)
    """

    @pytest.fixture
    def hotel_singapore(self):
        return {
            "id": "hotel_singapore",
            "name": "Singapore Hotel",
            "lat": 1.30,
            "lon": 103.85,
        }

    @pytest.fixture
    def hotel_johor(self):
        return {
            "id": "hotel_johor",
            "name": "Johor Hotel",
            "lat": 1.46,
            "lon": 103.76,
        }

    def test_first_city_multi_day_not_last(self, hotel_singapore):
        """First city (not last) has check-in on day 0, STAY on day 1."""
        events = determine_hotel_events(
            num_days=2,
            hotel=hotel_singapore,
            is_first_city=True,
            is_last_city=False,
        )

        # Check-in on day 0
        assert 0 in events
        assert any(e.event_type == HotelEventType.CHECK_IN for e in events[0])
        assert not any(e.event_type == HotelEventType.CHECK_OUT for e in events[0])

        # STAY on day 1 (checkout handled by next city)
        assert 1 in events
        assert any(e.event_type == HotelEventType.STAY for e in events[1])

    def test_last_city_single_day_checkout_from_prev(self, hotel_singapore, hotel_johor):
        """Last city (single day) only has checkout from previous hotel."""
        events = determine_hotel_events(
            num_days=1,
            hotel=hotel_johor,
            is_first_city=False,
            is_last_city=True,
            prev_city_hotel=hotel_singapore,
        )

        # Only checkout from Singapore hotel
        assert 0 in events
        assert len(events[0]) == 1
        event = events[0][0]
        assert event.event_type == HotelEventType.CHECK_OUT
        assert event.hotel_id == hotel_singapore["id"]

        # No check-in to Johor hotel (single day, not staying)
        checkins = [e for e in events.get(0, []) if e.event_type == HotelEventType.CHECK_IN]
        assert len(checkins) == 0

    def test_multi_city_global_checkin_checkout_balance(self, hotel_singapore, hotel_johor):
        """Verify num_checkin == num_checkout across all cities."""
        # Singapore: 2 days, first city, not last
        events_sg = determine_hotel_events(
            num_days=2,
            hotel=hotel_singapore,
            is_first_city=True,
            is_last_city=False,
        )

        # Johor: 1 day, not first, last city
        events_jb = determine_hotel_events(
            num_days=1,
            hotel=hotel_johor,
            is_first_city=False,
            is_last_city=True,
            prev_city_hotel=hotel_singapore,
        )

        # Count all events
        all_checkins = 0
        all_checkouts = 0

        for day_events in events_sg.values():
            for e in day_events:
                if e.event_type == HotelEventType.CHECK_IN:
                    all_checkins += 1
                elif e.event_type == HotelEventType.CHECK_OUT:
                    all_checkouts += 1

        for day_events in events_jb.values():
            for e in day_events:
                if e.event_type == HotelEventType.CHECK_IN:
                    all_checkins += 1
                elif e.event_type == HotelEventType.CHECK_OUT:
                    all_checkouts += 1

        assert all_checkins == all_checkouts == 1

    def test_multi_city_three_cities(self, hotel_singapore, hotel_johor):
        """Test 3-city trip: Singapore (2d) -> Johor (2d) -> KL (1d)."""
        hotel_kl = {
            "id": "hotel_kl",
            "name": "KL Hotel",
            "lat": 3.14,
            "lon": 101.69,
        }

        # Singapore: 2 days, first city, not last
        events_sg = determine_hotel_events(
            num_days=2,
            hotel=hotel_singapore,
            is_first_city=True,
            is_last_city=False,
        )

        # Johor: 2 days, not first, not last
        events_jb = determine_hotel_events(
            num_days=2,
            hotel=hotel_johor,
            is_first_city=False,
            is_last_city=False,
            prev_city_hotel=hotel_singapore,
        )

        # KL: 1 day, not first, last city
        events_kl = determine_hotel_events(
            num_days=1,
            hotel=hotel_kl,
            is_first_city=False,
            is_last_city=True,
            prev_city_hotel=hotel_johor,
        )

        # Singapore: check-in on day 0, STAY on day 1
        assert 0 in events_sg
        assert any(
            e.event_type == HotelEventType.CHECK_IN and e.hotel_id == hotel_singapore["id"] for e in events_sg[0]
        )
        assert 1 in events_sg
        assert any(e.event_type == HotelEventType.STAY for e in events_sg[1])

        # Johor: checkout from SG on day 0, check-in to JB on day 0, STAY on day 1
        assert 0 in events_jb
        assert any(
            e.event_type == HotelEventType.CHECK_OUT and e.hotel_id == hotel_singapore["id"] for e in events_jb[0]
        )
        assert any(e.event_type == HotelEventType.CHECK_IN and e.hotel_id == hotel_johor["id"] for e in events_jb[0])
        assert 1 in events_jb
        assert any(e.event_type == HotelEventType.STAY for e in events_jb[1])

        # KL: checkout from JB on day 0, no check-in
        assert 0 in events_kl
        assert len(events_kl[0]) == 1
        assert events_kl[0][0].event_type == HotelEventType.CHECK_OUT
        assert events_kl[0][0].hotel_id == hotel_johor["id"]

        # Count all events - should be balanced
        all_checkins = 0
        all_checkouts = 0

        for events in [events_sg, events_jb, events_kl]:
            for day_events in events.values():
                for e in day_events:
                    if e.event_type == HotelEventType.CHECK_IN:
                        all_checkins += 1
                    elif e.event_type == HotelEventType.CHECK_OUT:
                        all_checkouts += 1

        assert all_checkins == all_checkouts == 2
