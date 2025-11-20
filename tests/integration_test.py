import os
import json
from app.services.transformers import transform_frontend_payload
from app.services.maut import run_pipeline
from app.services.cvrptw import run_cvrptw
from app.utils.validators import assert_itinerary_valid


def test_full_pipeline():
    """Test complete pipeline: Frontend payload → MAUT → CVRPTW"""

    # 1. Frontend payload
    frontend_payload = {
        "title": "Singapore Test Trip",
        "destination": "Singapore",
        "dates": {
            "type": "specific",
            "startDate": "2025-06-01",
            "endDate": "2025-06-01",
        },
        "num_days": 1,
        "travelers": {"adults": 2, "children": 1, "pets": 1},
        "preferences": {
            "budget": "sensible",
            "pacing": "balanced",
            "interests": ["shopping", "cultural_history", "nature"],
        },
    }

    print("\n" + "=" * 60)
    print("STEP 1: Transform Frontend → MAUT Request")
    print("=" * 60)

    # 2. Transform to MAUT request
    maut_request = transform_frontend_payload(frontend_payload)
    print(f"✅ Destination: {maut_request['destination']}")
    print(f"✅ Num days: {maut_request['num_days']}")
    print(f"✅ Flags: {maut_request['flags']}")
    print(f"✅ Budget tier: {maut_request['budget_tier']}")

    # 3. Run MAUT
    print("\n" + "=" * 60)
    print("STEP 2: Run MAUT Pipeline")
    print("=" * 60)

    maut_output = run_pipeline(maut_request)
    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    print(f"✅ Status: {maut_output['status']}")
    print(f"✅ POIs returned: {len(places)}")
    print(f"✅ Selected themes: {meta.get('selected_themes', [])}")
    print(f"✅ Count in: {meta.get('count_in', 0)}")
    print(f"✅ Count out: {meta.get('count_out', 0)}")

    # Save MAUT output
    maut_output_path = os.path.join(
        os.path.dirname(__file__), "integration_maut_output.json"
    )
    with open(maut_output_path, "w", encoding="utf-8") as f:
        json.dump(maut_output, f, indent=2)

    # 4. Enrich MAUT output with dates and num_days for CVRPTW
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # 5. Run CVRPTW
    print("\n" + "=" * 60)
    print("STEP 3: Run CVRPTW")
    print("=" * 60)

    # Try to use accommodation from MAUT output, fallback to default hotel
    accommodations = [p for p in places if "accommodation" in p.get("poi_roles", [])]
    if accommodations:
        hotel_poi = accommodations[0]
        hotel = {
            "id": hotel_poi["id"],
            "name": hotel_poi["name"],
            "lat": hotel_poi.get("latitude")
            or hotel_poi.get("coordinates", {}).get("lat"),
            "lon": hotel_poi.get("longitude")
            or hotel_poi.get("coordinates", {}).get("lng"),
        }
        print(f"Using accommodation from MAUT: {hotel['name']}")
    else:
        hotel = {
            "id": "hotel_test",
            "name": "Test Hotel Singapore",
            "lat": 1.290270,
            "lon": 103.851959,
        }
        print("No accommodations in MAUT output, using default hotel")

    cvrptw_output = run_cvrptw(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=30,
    )

    if len(cvrptw_output.get("days", [])) == 0:
        print("❌ CVRPTW returned no days")
    else:
        print(f"✅ Days planned: {len(cvrptw_output.get('days', []))}")

    for i, day in enumerate(cvrptw_output.get("days", [])):
        print(
            f"   Day {i + 1} ({day['date']}): {len(day['stops'])} stops, {day['meals']} meals"
        )
        for stop in day["stops"][:3]:  # Show first 3 stops
            print(f"      - {stop['arrival']} {stop['name']} ({stop['role']})")

    # Save CVRPTW output
    cvrptw_output_path = os.path.join(
        os.path.dirname(__file__), "integration_cvrptw_output.json"
    )
    with open(cvrptw_output_path, "w", encoding="utf-8") as f:
        json.dump(cvrptw_output, f, indent=2)

    # 6. Validate itinerary
    print("\n" + "=" * 60)
    print("STEP 4: Validate Itinerary")
    print("=" * 60)

    # Validate with detailed report
    assert_itinerary_valid(
        cvrptw_output=cvrptw_output,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=True,  # Allow warnings, only fail on errors
    )

    # Comprehensive assertions
    assert maut_output is not None
    assert maut_output["status"] == "ok", f"MAUT failed: {maut_output.get('status')}"
    assert len(places) > 0, "MAUT returned no POIs"

    assert cvrptw_output is not None
    days = cvrptw_output.get("days", [])
    note = cvrptw_output.get("note")
    assert len(days) > 0, f"CVRPTW returned no days: {note}"

    # Validate day count and dates
    assert (
        len(days) == frontend_payload["num_days"]
    ), f"Expected {frontend_payload['num_days']} days, got {len(days)}"
    assert (
        days[0]["date"] == frontend_payload["dates"]["startDate"]
    ), "Start date mismatch"
    assert days[-1]["date"] == frontend_payload["dates"]["endDate"], "End date mismatch"

    # Validate per-day structure
    for i, day in enumerate(days):
        assert len(day["stops"]) >= 1, f"Day {i+1} has no stops"
        assert day["stops"][-1]["role"] == "hotel", f"Day {i+1} doesn't end at hotel"

    print("\n" + "=" * 60)
    print("✅ INTEGRATION TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    test_full_pipeline()
