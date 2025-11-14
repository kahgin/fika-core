import os
import json
from app.services.maut import run_pipeline
from app.services.cvrptw import run_cvrptw

MAUT_TEST_PATH = os.path.join(os.path.dirname(__file__), "maut_test.json")


def test_cvrptw_with_maut():
    """Test CVRPTW with MAUT output"""
    # Load MAUT test input
    with open(MAUT_TEST_PATH, "r", encoding="utf-8") as f:
        maut_request = json.load(f)

    # Run MAUT pipeline
    maut_output = run_pipeline(maut_request)

    # Save MAUT output
    maut_output_path = os.path.join(os.path.dirname(__file__), "maut_output.json")
    with open(maut_output_path, "w", encoding="utf-8") as f:
        json.dump(maut_output, f, indent=2)

    # Run CVRPTW
    hotel = {"id": "hotel_1", "name": "Test Hotel", "lat": 1.290270, "lon": 103.851959}

    cvrptw_output = run_cvrptw(
        maut_output=maut_output,
        hotel=hotel,
        pacing="balanced",
        mandatory=None,
        time_limit_sec=15,
    )

    # Save CVRPTW output
    cvrptw_output_path = os.path.join(os.path.dirname(__file__), "cvrptw_output.json")
    with open(cvrptw_output_path, "w", encoding="utf-8") as f:
        json.dump(cvrptw_output, f, indent=2)

    # Assertions
    assert cvrptw_output is not None
    assert isinstance(cvrptw_output, dict)
    assert "days" in cvrptw_output
    assert isinstance(cvrptw_output["days"], list)

    print(f"\n✅ MAUT output: {len(maut_output.get('places', []))} POIs")
    print(f"✅ CVRPTW output: {len(cvrptw_output['days'])} days")
    for i, day in enumerate(cvrptw_output["days"]):
        print(f"   Day {i + 1}: {len(day['stops'])} stops, {day['meals']} meals")


if __name__ == "__main__":
    test_cvrptw_with_maut()
