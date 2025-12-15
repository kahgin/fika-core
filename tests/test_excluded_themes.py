from app.services.transformers import transform_frontend_payload


def _base_payload():
    return {
        "destination": "Singapore",
        "dates": {"type": "flexible", "days": 3},
        "preferences": {"interests": ["cultural_history", "nature"], "pacing": "balanced"},
        "flags": {},
    }


def test_explicit_excludes_used_as_is():
    payload = _base_payload()
    payload["excluded_themes"] = ["nightlife", "family", "nightlife"]
    out = transform_frontend_payload(payload)
    # Deduped, order preserved for first occurrence
    assert out["excluded_themes"] == ["nightlife", "family"]


def test_empty_excludes_injects_nightlife_when_muslim():
    payload = _base_payload()
    payload["flags"] = {"is_muslim": True}
    payload["excluded_themes"] = []
    out = transform_frontend_payload(payload)
    # User provided empty excludes; still ensure nightlife excluded
    assert out["excluded_themes"] == ["nightlife"]


def test_is_muslim_defaults_to_nightlife_when_not_provided():
    payload = _base_payload()
    payload["flags"] = {"is_muslim": True}
    # excluded_themes not present
    out = transform_frontend_payload(payload)
    assert out["excluded_themes"] == ["nightlife"]


def test_not_muslim_and_not_provided_results_in_empty_excludes():
    payload = _base_payload()
    # excluded_themes not present; is_muslim False/omitted
    out = transform_frontend_payload(payload)
    assert out["excluded_themes"] == []


def test_is_muslim_and_explicit_list_still_excludes_nightlife():
    payload = _base_payload()
    payload["flags"] = {"is_muslim": True}
    payload["excluded_themes"] = ["family"]
    out = transform_frontend_payload(payload)
    # Ensure nightlife is excluded even if not specified explicitly
    assert out["excluded_themes"] == ["family", "nightlife"]
