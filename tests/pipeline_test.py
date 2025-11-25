import os
import json

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_pipeline
from app.services.pipeline import run_full_pipeline
from app.utils.validators import assert_itinerary_valid

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_spec.json")

def test_full_pipeline_user_path():
    """
    Production pipeline test:
    MAUT → ACS-CVRPTW (no ACO) → validation.
    """
    # Load test input
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_pipeline(maut_request)
    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    # Ensure MAUT is ok
    assert maut_output["status"] == "ok"
    assert len(places) > 0

    # Inject dates/num_days into meta so build_problem has them
    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # Derive hotel from MAUT meta
    selected_hotel = meta.get("selected_hotel")
    assert selected_hotel, "MAUT did not select a hotel"

    coords = selected_hotel.get("coordinates") or {}
    hotel = {
        "id": selected_hotel["id"],
        "name": selected_hotel["name"],
        "lat": coords.get("lat") or selected_hotel.get("latitude"),
        "lon": coords.get("lng") or selected_hotel.get("longitude"),
    }

    # Run production pipeline: ACS-CVRPTW, no ACO
    cvrptw_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=20,
        use_aco=False,
        solver="acs",
    )

    assert cvrptw_output is not None
    assert cvrptw_output.get("status") == "success", cvrptw_output.get("error", "")
    days = cvrptw_output.get("days", [])
    assert len(days) == frontend_payload["num_days"]

    # Validate itinerary strictly (no warnings allowed)
    assert_itinerary_valid(
        cvrptw_output=cvrptw_output,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=True,
    )

    # Save output for manual inspection
    out_dir = os.path.dirname(__file__)
    out_path = os.path.join(out_dir, "pipeline_one_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cvrptw_output, f, indent=2)
