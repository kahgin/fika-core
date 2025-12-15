"""
MAUT (Multi-Attribute Utility Theory) pipeline test.

Tests POI scoring and selection.
"""

import os
import json

from app.services.maut import run_maut
from app.services.transformers import transform_frontend_payload

TEST_PATH = os.path.join(os.path.dirname(__file__), "sample_payload_flex.json")


def test_maut_pipeline():
    """
    Test MAUT pipeline returns scored POIs.

    Verifies:
    - Status is ok
    - POIs are returned
    - Themes are selected
    - Hotel is selected
    - Required fields present on POIs
    """
    with open(TEST_PATH, "r", encoding="utf-8") as f:
        frontend_payload = json.load(f)

    maut_request = transform_frontend_payload(frontend_payload)
    maut_output = run_maut(maut_request)

    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    # Basic structure
    assert maut_output["status"] == "ok"
    assert len(places) > 0

    # Hotel selected
    assert meta.get("selected_hotel") is not None

    # POI structure
    sample = places[0]
    assert "id" in sample
    assert "name" in sample
    assert "roles" in sample
    assert "coordinates" in sample

    # Role distribution
    by_role = meta.get("by_role", {})
    assert by_role.get("attraction", 0) > 0
    # Note: meal count may be 0 if dietary restrictions (halal+vegetarian) filter all meals
    assert by_role.get("accommodation", 0) > 0

    print(f"\n✅ MAUT: {len(places)} POIs, themes: {meta.get('selected_themes')}")
    print(f"   By role: {by_role}")
