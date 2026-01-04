"""
Tests for hotel event handling in VRP model.

RULES:
1. Single-day single-city trip: NO hotel events
2. Each hotel has ONE check-in and ONE check-out (paired)
3. Check-in on FIRST day of city segment
4. Check-out on FIRST day of NEXT city (transition) OR last day if last city
5. Single-day LAST destination: Only check-out from PREVIOUS hotel
6. STAY events for intermediate days
7. num_checkin == num_checkout globally
"""

import pytest
import datetime as dt
from app.services.vrp_model import DaySpec, HotelEvent, HotelEventType
from app.services.vrp_utils import determine_hotel_events, create_hotel_event_nodes


@pytest.fixture
def hotel():
    return {"id": "hotel1", "name": "Test Hotel", "lat": 1.3, "lon": 103.8}


@pytest.fixture
def prev_hotel():
    return {"id": "prev_hotel", "name": "Previous Hotel", "lat": 1.2, "lon": 103.7}


class TestDetermineHotelEvents:
    """Tests for determine_hotel_events function."""

    def test_single_day_single_city_no_events(self, hotel):
        """Rule 1: Single-day trip has NO hotel events."""
        events = determine_hotel_events(num_days=1, hotel=hotel, is_first_city=True, is_last_city=True)
        assert len(events) == 0

    def test_multi_day_first_last_city(self, hotel):
        """Multi-day trip has checkin on day 0, checkout on last day."""
        events = determine_hotel_events(num_days=3, hotel=hotel, is_first_city=True, is_last_city=True)

        # Day 0: checkin
        assert 0 in events
        assert HotelEventType.CHECK_IN in [e.event_type for e in events[0]]

        # Day 1: stay
        assert 1 in events
        assert HotelEventType.STAY in [e.event_type for e in events[1]]

        # Day 2: checkout
        assert 2 in events
        assert HotelEventType.CHECK_OUT in [e.event_type for e in events[2]]

    def test_transition_day_has_both(self, hotel, prev_hotel):
        """Transition day has checkout from prev + checkin to current."""
        events = determine_hotel_events(
            num_days=2, hotel=hotel, is_first_city=False, is_last_city=True, prev_city_hotel=prev_hotel
        )

        day0_types = [e.event_type for e in events[0]]
        assert HotelEventType.CHECK_OUT in day0_types
        assert HotelEventType.CHECK_IN in day0_types

        checkout = next(e for e in events[0] if e.event_type == HotelEventType.CHECK_OUT)
        checkin = next(e for e in events[0] if e.event_type == HotelEventType.CHECK_IN)
        assert checkout.hotel_id == prev_hotel["id"]
        assert checkin.hotel_id == hotel["id"]

    def test_single_day_last_only_checkout(self, hotel, prev_hotel):
        """Rule 5: Single-day last city only has checkout from prev."""
        events = determine_hotel_events(
            num_days=1, hotel=hotel, is_first_city=False, is_last_city=True, prev_city_hotel=prev_hotel
        )

        assert 0 in events
        assert len(events[0]) == 1
        assert events[0][0].event_type == HotelEventType.CHECK_OUT
        assert events[0][0].hotel_id == prev_hotel["id"]

    def test_checkin_checkout_count_equal(self, hotel):
        """Rule 7: num_checkin == num_checkout."""
        events = determine_hotel_events(num_days=5, hotel=hotel, is_first_city=True, is_last_city=True)

        all_events = [e for day_events in events.values() for e in day_events]
        checkins = [e for e in all_events if e.event_type == HotelEventType.CHECK_IN]
        checkouts = [e for e in all_events if e.event_type == HotelEventType.CHECK_OUT]
        assert len(checkins) == len(checkouts)


class TestDaySpecProperties:
    """Tests for DaySpec hotel event properties."""

    def test_has_hotel_event(self):
        day = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="h1",
            hotel_events=[HotelEvent(HotelEventType.CHECK_IN, "h1", "Hotel", 1.3, 103.8, (14 * 60, 16 * 60))],
        )
        assert day.has_hotel_event is True

    def test_has_check_in(self):
        day = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="h1",
            hotel_events=[HotelEvent(HotelEventType.CHECK_IN, "h1", "Hotel", 1.3, 103.8, (14 * 60, 16 * 60))],
        )
        assert day.has_check_in is True
        assert day.has_check_out is False

    def test_is_transition_day(self):
        day = DaySpec(
            day_index=0,
            date=dt.date(2025, 1, 15),
            start_min=9 * 60,
            end_min=20 * 60,
            depot_id="h1",
            hotel_events=[
                HotelEvent(HotelEventType.CHECK_OUT, "h0", "Prev", 1.2, 103.7, (10 * 60, 12 * 60)),
                HotelEvent(HotelEventType.CHECK_IN, "h1", "Hotel", 1.3, 103.8, (14 * 60, 16 * 60)),
            ],
        )
        assert day.is_transition_day is True


class TestCreateHotelEventNodes:
    """Tests for create_hotel_event_nodes function."""

    def test_creates_nodes_with_coords(self):
        day_specs = [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="h1",
                hotel_events=[
                    HotelEvent(HotelEventType.CHECK_IN, "h1", "Hotel", 1.3, 103.8, (14 * 60, 16 * 60), service_time=30)
                ],
            )
        ]
        nodes, _ = create_hotel_event_nodes(day_specs, start_idx=1)

        assert len(nodes) == 1
        assert nodes[0].lat == 1.3
        assert nodes[0].lon == 103.8
        assert nodes[0].is_mandatory is True
        assert nodes[0].role == "accommodation"

    def test_stay_not_mandatory(self):
        day_specs = [
            DaySpec(
                day_index=0,
                date=dt.date(2025, 1, 15),
                start_min=9 * 60,
                end_min=20 * 60,
                depot_id="h1",
                hotel_events=[HotelEvent(HotelEventType.STAY, "h1", "Hotel", 1.3, 103.8, (0, 24 * 60), service_time=0)],
            )
        ]
        nodes, _ = create_hotel_event_nodes(day_specs, start_idx=1)

        assert len(nodes) == 1
        assert nodes[0].is_mandatory is False
