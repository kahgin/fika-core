"""
End-to-end pipeline test.

Tests the full production path: MAUT → ACS-CVRPTW → Validation.
"""

import os
import json

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.utils.validators import assert_itinerary_valid

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_flex.json")


def test_full_pipeline():
    """
    Production pipeline test: MAUT → ACS-CVRPTW → Validation.

    Verifies:
    - MAUT returns POIs
    - Pipeline generates correct number of days
    - Itinerary passes validation
    """
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    # Transform and run MAUT
    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_maut(maut_request)

    assert maut_output["status"] == "ok"
    assert len(maut_output.get("places", [])) > 0

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

    # Run pipeline with ACS solver
    result = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        solver="acs",
    )

    # Save output
    out_path = os.path.join(os.path.dirname(__file__), "pipeline_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    # Assertions
    assert result.get("status") == "success", result.get("error", "")
    assert len(result.get("days", [])) == maut_request["num_days"]

    # Validate
    assert_itinerary_valid(
        cvrptw_output=result,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=True,
    )
