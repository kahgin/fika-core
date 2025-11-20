import os
import json
from app.services.maut import run_pipeline

MAUT_TEST_PATH = os.path.join(os.path.dirname(__file__), "maut_test.json")


def test_maut_pipeline():
    """Test MAUT pipeline execution"""
    # Load test input
    with open(MAUT_TEST_PATH, "r", encoding="utf-8") as f:
        maut_request = json.load(f)

    # Run pipeline
    maut_output = run_pipeline(maut_request)

    # Save output
    output_path = os.path.join(os.path.dirname(__file__), "maut_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(maut_output, f, indent=2)

    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    # Assertions
    assert maut_output is not None
    assert isinstance(maut_output, dict)
    assert "status" in maut_output
    assert "places" in maut_output
    assert "meta" in maut_output

    assert (
        maut_output["status"] == "ok"
    ), f"MAUT failed with status: {maut_output.get('status')}"

    assert len(places) > 0, "MAUT returned zero places for maut_test.json"
    assert isinstance(meta.get("selected_themes", []), list)
    assert len(meta.get("selected_themes", [])) > 0, "No themes selected"
    assert meta.get("count_in", 0) >= meta.get(
        "count_out", 0
    ), "count_in should be >= count_out"

    # Validate required fields on first POI
    if places:
        sample = places[0]
        for key in ("id", "name", "poi_roles"):
            assert key in sample, f"MAUT place missing required field '{key}'"
        assert (
            "latitude" in sample or "coordinates" in sample
        ), "POI missing location data"

    # Only available with "pytest -s tests/maut_test.py"
    print("\n✅ MAUT pipeline completed")
    print(f"   Status: {maut_output['status']}")
    print(f"   POIs returned: {len(places)}")
    print(f"   Selected themes: {meta.get('selected_themes', [])}")
    print(f"   Count in: {meta.get('count_in', 0)}")
    print(f"   Count out: {meta.get('count_out', 0)}")


if __name__ == "__main__":
    test_maut_pipeline()
