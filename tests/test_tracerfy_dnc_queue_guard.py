"""Fail-closed identity and membership checks for paid Tracerfy DNC queues."""

import json

import pytest

import scripts.skip_trace_tracerfy as tracerfy


def result(phone, clean=True):
    return {
        "phone": phone,
        "is_clean": clean,
        "national_dnc": not clean,
        "state_dnc": False,
        "dma": False,
        "litigator": False,
    }


def inline_payload(queue_id="2353", rows=None):
    return json.dumps({
        "id": queue_id,
        "pending": False,
        "results": rows if rows is not None else [],
    }).encode()


def test_matching_queue_identity_and_nanp_membership_are_accepted(monkeypatch):
    rows = [result("2695550101"), result("2695550102")]
    monkeypatch.setattr(
        tracerfy,
        "_request",
        lambda *args, **kwargs: inline_payload(rows=rows),
    )
    assert tracerfy.poll_dnc(
        "2353", "test-key", cap=0,
        expected_phones=["12695550101", "2695550102"],
    ) == rows


def test_wrong_queue_identity_fails_before_results_are_accepted(monkeypatch):
    monkeypatch.setattr(
        tracerfy,
        "_request",
        lambda *args, **kwargs: inline_payload("999", [result("2695550101")]),
    )
    with pytest.raises(tracerfy.TracerfyContractError, match=(
            "queue identity mismatch: requested=2353 returned=999")):
        tracerfy.poll_dnc("2353", "test-key", cap=0)


@pytest.mark.parametrize(
    "payload",
    (
        {"pending": False, "results": [result("2695550101")]},
        [result("2695550101")],
        {"id": 2353, "queue_id": 999, "pending": False,
         "results": [result("2695550101")]},
    ),
)
def test_missing_bare_or_conflicting_queue_identity_fails_closed(monkeypatch, payload):
    monkeypatch.setattr(
        tracerfy,
        "_request",
        lambda *args, **kwargs: json.dumps(payload).encode(),
    )
    with pytest.raises(tracerfy.TracerfyContractError):
        tracerfy.poll_dnc("2353", "test-key", cap=0)


def test_identity_is_verified_before_following_download_url(monkeypatch):
    calls = []

    def fake_request(method, url, key, **kwargs):
        calls.append((url, kwargs.get("auth")))
        if len(calls) == 1:
            return json.dumps({
                "id": 2353,
                "pending": False,
                "download_url": "https://download.invalid/queue2353.csv",
            }).encode()
        return b"phone,is_clean,national_dnc,state_dnc,dma,litigator\n2695550101,true,false,false,false,false\n"

    monkeypatch.setattr(tracerfy, "_request", fake_request)
    rows = tracerfy.poll_dnc(
        "2353", "test-key", cap=0, expected_phones=["12695550101"])
    assert len(rows) == 1
    assert calls == [
        (f"{tracerfy.TRACERFY_BASE}/dnc/queue/2353", None),
        ("https://download.invalid/queue2353.csv", False),
    ]


def test_membership_mismatch_prints_queue_and_exact_sets(monkeypatch):
    rows = [result("2695550101"), result("2695550199")]
    monkeypatch.setattr(
        tracerfy,
        "_request",
        lambda *args, **kwargs: inline_payload(rows=rows),
    )
    with pytest.raises(tracerfy.TracerfyContractError) as caught:
        tracerfy.poll_dnc(
            "2353", "test-key", cap=0,
            expected_phones=["12695550101", "12695550102"],
        )
    message = str(caught.value)
    assert "queue_id=2353" in message
    assert "missing=['2695550102']" in message
    assert "extra=['2695550199']" in message


@pytest.mark.parametrize(
    "rows",
    (
        [result("2695550101"), result("12695550101")],
        [result("2695550101"), {"is_clean": True}],
    ),
)
def test_duplicate_alias_or_missing_phone_fails_closed(monkeypatch, rows):
    monkeypatch.setattr(
        tracerfy,
        "_request",
        lambda *args, **kwargs: inline_payload(rows=rows),
    )
    with pytest.raises(tracerfy.TracerfyContractError):
        tracerfy.poll_dnc(
            "2353", "test-key", cap=0,
            expected_phones=["2695550101", "2695550102"],
        )
