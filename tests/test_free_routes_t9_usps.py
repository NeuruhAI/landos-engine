import pytest
from app.free_routes.usps import (credentials_present, validate_many, validate_one, dpv_to_confidence, HUMAN_STEP, SOURCE)


def test_no_credentials_is_a_human_step_not_a_fabricated_address():
    assert credentials_present({}) is False
    with pytest.raises(PermissionError) as e:
        validate_many([{"key": "a", "street": "1 Main St"}], env={}, today="2026-09-02", now_iso="2026-09-02T19:00:00Z")
    assert "cop.usps.com" in str(e.value) and HUMAN_STEP in str(e.value)


def test_validation_stamps_source_as_of_and_confidence_from_dpv():
    def post(url, data):
        assert data["grant_type"] == "client_credentials" and data["client_id"] == "k"
        return {"access_token": "tok"}
    def get(url, token):
        assert token == "tok" and "streetAddress=605+Utley+St" in url
        return {"address": {"streetAddress": "605 UTLEY ST", "city": "HOLLY SPRINGS", "state": "NC", "ZIPCode": "27540", "ZIPPlus4": "9169"},
                "additionalInfo": {"DPVConfirmation": "Y", "vacant": "N"}}
    out = validate_many([{"key": "c1", "street": "605 Utley St", "city": "Holly Springs", "state": "NC"}],
                        env={"USPS_CONSUMER_KEY": "k", "USPS_CONSUMER_SECRET": "s"}, today="2026-09-02",
                        now_iso="2026-09-02T19:00:00Z", post=post, get=get, delay_s=0)
    v = out[0]
    assert v.standardized == "605 UTLEY ST" and v.zip5 == "27540" and v.zip4 == "9169"
    assert v.deliverable is True and v.vacant is False and v.confidence == "HIGH"
    assert v.source == SOURCE and v.source_as_of == "2026-09-02" and v.observed_at == "2026-09-02T19:00:00Z"


def test_api_failure_is_recorded_not_invented():
    def get(url, token):
        raise RuntimeError("HTTP 429 quota")
    v = validate_one("c2", "1 Nowhere", token="t", today="2026-09-02", now_iso="x", get=get)
    assert v.standardized is None and v.deliverable is None and v.error.startswith("HTTP 429")
    assert dpv_to_confidence("N") == "LOW" and dpv_to_confidence(None) is None
