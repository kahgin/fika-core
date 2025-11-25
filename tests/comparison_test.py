import os
import json

from app.services.transformers import transform_frontend_payload
from app.services.maut import run_pipeline
from app.services.pipeline import run_full_pipeline
from app.utils.validators import assert_itinerary_valid

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_spec.json")

def test_compare_ortools_vs_acs():
    """
    Compare OR-Tools CVRPTW vs ACS-CVRPTW on the same MAUT output and OSRM matrix.

    Both paths:
      - Use OSRM-based travel times (via build_problem).
      - Use the same hotel, dates, pacing, and candidate POIs.
      - Are validated with the same validator.
    """
    # Load test input
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_pipeline(maut_request)
    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    assert maut_output["status"] == "ok"
    assert len(places) > 0

    maut_output.setdefault("meta", {})
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    selected_hotel = meta.get("selected_hotel")
    assert selected_hotel, "MAUT did not select a hotel"

    coords = selected_hotel.get("coordinates") or {}
    hotel = {
        "id": selected_hotel["id"],
        "name": selected_hotel["name"],
        "lat": coords.get("lat") or selected_hotel.get("latitude"),
        "lon": coords.get("lng") or selected_hotel.get("longitude"),
    }

    # OR-Tools baseline (no ACO)
    ortools_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=20,
        use_aco=False,
        solver="ortools",
    )
    assert ortools_output.get("status") == "success", ortools_output.get("error", "")
    assert_itinerary_valid(
        cvrptw_output=ortools_output,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=True,
    )

    # ACS-CVRPTW
    acs_output = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=20,
        use_aco=False,  # ACS already does routing; no ACO here
        solver="acs",
    )
    assert acs_output.get("status") == "success", acs_output.get("error", "")
    assert_itinerary_valid(
        cvrptw_output=acs_output,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=True,
    )

    # Compare distances (both measured with OSRM in pipeline)
    d_ortools = ortools_output["meta"]["total_distance"]
    d_acs = acs_output["meta"]["total_distance"]

    # Loosely assert ACS isn't catastrophically worse
    if d_ortools > 0:
        assert d_acs <= d_ortools * 1.5, (
            f"ACS distance {d_acs:.2f}km is much worse than OR-Tools {d_ortools:.2f}km"
        )

    # Save outputs to inspect
    out_dir = os.path.dirname(__file__)
    with open(
        os.path.join(out_dir, "comparison_ortools_output.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(ortools_output, f, indent=2)
    with open(
        os.path.join(out_dir, "comparison_acs_output.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(acs_output, f, indent=2)
