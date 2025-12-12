"""
OR-Tools CVRPTW solver test.

Tests the OR-Tools constraint solver directly.
"""

import os
import json

from app.services.maut import run_maut
from app.services.transformers import transform_frontend_payload
from app.services.cvrptw import run_cvrptw

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_spec.json")


def test_cvrptw_solver():
    """
    Test OR-Tools CVRPTW solver.

    Verifies:
    - Returns days structure
    - Correct number of days
    - Each day has stops
    - Last stop is depot/hotel
    """
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    # Run MAUT
    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_maut(maut_request)

    # Inject dates/num_days
    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # Get hotel
    selected_hotel = maut_output["meta"].get("selected_hotel")
    assert selected_hotel, "MAUT did not select a hotel"

    coords = selected_hotel.get("coordinates") or {}
    hotel = {
        "id": selected_hotel["id"],
        "name": selected_hotel["name"],
        "lat": coords.get("lat"),
        "lon": coords.get("lng"),
    }

    # Run CVRPTW
    result = run_cvrptw(
        maut_output=maut_output,
        hotel=hotel,
        pacing="balanced",
    )

    # Save output
    out_path = os.path.join(os.path.dirname(__file__), "cvrptw_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    # Assertions
    assert "days" in result
    days = result["days"]
    assert len(days) > 0, f"No days returned: {result.get('note')}"
    assert len(days) == maut_request["num_days"]

    # Each day has stops ending at depot
    for i, day in enumerate(days):
        assert len(day["stops"]) >= 1, f"Day {i + 1} has no stops"
        last = day["stops"][-1]
        assert last["role"] in ("depot", "hotel"), f"Day {i + 1} doesn't end at depot"

    print(f"\n✅ CVRPTW: {len(days)} days")
    for i, day in enumerate(days):
        print(f"   Day {i + 1}: {len(day['stops'])} stops, {day.get('meals', 0)} meals")
