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

    # Assertions
    assert maut_output is not None
    assert isinstance(maut_output, dict)
    assert "status" in maut_output
    assert "places" in maut_output
    assert "meta" in maut_output

    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    print(f"\n✅ MAUT pipeline completed")
    print(f"   Status: {maut_output['status']}")
    print(f"   POIs returned: {len(places)}")
    print(f"   Selected themes: {meta.get('selected_themes', [])}")
    print(f"   Count in: {meta.get('count_in', 0)}")
    print(f"   Count out: {meta.get('count_out', 0)}")


if __name__ == "__main__":
    test_maut_pipeline()
