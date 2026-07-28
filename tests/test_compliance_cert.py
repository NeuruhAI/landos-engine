"""
test_compliance_cert.py — unit tests for the COMPLIANCE CERTIFICATE GENERATOR
(app/compliance_cert.py). Pure, no DB / no network: builds real lead-receipt spines with
in-memory fixtures (same idiom as test_receipt.py), then certifies them.

Locks the properties the directive demands:
  * every attestation line traces to a REAL receipt hash (a section hash or the chain hash)
  * the org name is CONFIG-DRIVEN (white-label): swap org -> the certificate + HTML change
  * the document is CLIENT-CLEAN (light theme, self-contained, professional) — not terminal
  * SOC2-ADJACENT framing is present and honest
  * the certificate is SIGNED and independently verifiable; tamper breaks the signature
  * DNC/TCPA/freshness attestations reflect the receipt truthfully (pass / flag / n-a)
  * batch certification rolls up under a batch-root hash and flags dirty leads
  * no third-party PII (relative names / phone digits) leaks into the certificate
"""
import json
from datetime import date

from app.receipt import (
    SECTION_ORDER,
    build_chain,
    build_signal_section,
    build_scoring_section,
    build_pierce_section,
    build_compliance_section,
    build_freshness_section,
)
from app.compliance_cert import (
    build_certificate,
    build_batch_certificate,
    render_certificate_html,
    render_batch_html,
    verify_certificate,
    certificate_signing_payload,
    _sha256,
)
from app.receipt import canonical_json

ORG_A = {"org_name": "APG Land", "org_url": "https://apgland.example", "tagline": "Hunt. Fund. Close."}
ORG_B = {"org_name": "Reseller Diligence Co", "org_url": None, "tagline": None}
KEY = "shared-buyer-secret-key"


def _make_receipt(dnc=False, as_of=date(2026, 7, 5), lead_id="aaaaaaaa-0000-0000-0000-000000000001",
                  apn="R12345"):
    """A full receipt shell (shape == app.receipt.assemble_receipt output) built purely."""
    signals = [{
        "source_type": "taxsale", "source_ref": "WILCO-2026-07-07-T01",
        "source_url": "https://mvbalaw.example/x.pdf", "county_fips": "48491",
        "signal_date": date(2026, 7, 7), "signal_class": "temporal", "status": "matched",
        "ingested_at": "2026-07-04T00:00:00Z",
        "raw_payload": {"owner": "SECRET OWNER LLC", "min_bid": 2500}, "owner_name": "SECRET OWNER LLC",
    }]
    lead = {"final_lead_score": 41.0, "emerging_score": 32.5, "absentee_score": None,
            "confidence_score": None, "provenance": "discovered_taxsale", "source": "taxsale",
            "owner_name": "SECRET OWNER LLC", "skip_trace_status": None, "county_fips": "48491"}
    relations = [{"relative_name": "Jane Q Heir", "relationship": "daughter", "phone": "5125551234",
                  "phone_type": "mobile", "is_heir": True, "confidence": 0.82, "source": "tracerfy",
                  "added_at": "2026-07-01T00:00:00Z"}]
    acq = {"lead_id": lead_id, "dnc": dnc, "call_status": "new", "status": "discovered",
           "phone_1": "5125551234", "phone_1_type": "mobile", "phone_2": None, "last_call_at": None}
    sections = {
        "SIGNAL": build_signal_section(signals),
        "SCORING": build_scoring_section(lead, None, None),
        "PIERCE": build_pierce_section(relations),
        "COMPLIANCE": build_compliance_section(acq, lead),
        "FRESHNESS": build_freshness_section(signals, lead, as_of),
    }
    spine = build_chain(sections)
    return {
        "receipt_version": "1.0", "generated_by": "landos-engine/lead-receipt",
        "lead_id": lead_id, "parcel_apn": apn, "county_fips": "48491",
        "owner_name": "SECRET OWNER LLC", "situs_address": "123 Rural Rd",
        "as_of": as_of.isoformat(), "hash_algo": "sha256", "section_order": list(SECTION_ORDER),
        "section_hashes": spine["section_hashes"], "chain_sha256": spine["chain_sha256"],
        "sections": spine["sections"],
    }


def test_every_attestation_traces_to_a_real_receipt_hash():
    r = _make_receipt()
    cert = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    real_hashes = set(r["section_hashes"].values()) | {r["chain_sha256"]}
    assert len(cert["attestations"]) == 5
    for a in cert["attestations"]:
        assert a["receipt_hash"] in real_hashes, f'{a["gate"]} hash not from the receipt'
    # the summary agrees that all lines are verifiable
    assert cert["summary"]["all_verifiable"] is True
    # provenance line is bound to the top-level chain hash specifically
    prov = next(a for a in cert["attestations"] if a["gate"] == "Data provenance")
    assert prov["receipt_hash"] == r["chain_sha256"]


def test_org_name_is_config_driven():
    r = _make_receipt()
    a = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    b = build_certificate(r, ORG_B, KEY, issued_at="2026-07-06")
    assert a["issuer"]["org_name"] == "APG Land"
    assert b["issuer"]["org_name"] == "Reseller Diligence Co"
    # nothing is hardcoded: the two certs differ and their HTML carries the right brand
    assert a != b
    ha, hb = render_certificate_html(a), render_certificate_html(b)
    assert "APG Land" in ha and "Reseller Diligence Co" not in ha
    assert "Reseller Diligence Co" in hb and "APG Land" not in hb
    # signatures differ because the issuer block is inside the signed body
    assert a["signature"]["value"] != b["signature"]["value"]


def test_html_is_client_clean_and_self_contained():
    r = _make_receipt()
    html = render_certificate_html(build_certificate(r, ORG_A, KEY, issued_at="2026-07-06"))
    assert "Compliance Certificate" in html and "<style" in html
    # light / professional theme, NOT the terminal-dark engine aesthetic
    assert "--bg:#ffffff" in html
    assert "#0a0a0a" not in html and "background:#000" not in html
    # fully self-contained: no external scripts / stylesheets / remote assets
    for banned in ("<script", "<link", " src=", "cdn."):
        assert banned not in html, f"non-self-contained token present: {banned}"
    # valid document shell
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")


def test_soc2_framing_present_and_honest():
    r = _make_receipt()
    cert = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    s = cert["soc2_statement"].lower()
    assert "compliance gate" in s
    assert "independently verifiable" in s
    assert "cryptographically" in s or "sha-256" in s
    # honesty guardrail: it must disclaim being a framework certification
    assert "not a framework certification" in s


def test_signature_hmac_and_content_paths_verify():
    r = _make_receipt()
    # HMAC path (buyer holds the key)
    signed = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    assert signed["signature"]["algo"] == "hmac-sha256"
    assert verify_certificate(signed, KEY) is True
    assert verify_certificate(signed, "wrong-key") is False
    # content-hash path (no key)
    sealed = build_certificate(r, ORG_A, None, issued_at="2026-07-06")
    assert sealed["signature"]["algo"] == "sha256-content"
    assert verify_certificate(sealed, None) is True
    # the content hash equals sha256 of the canonical signing payload
    assert sealed["signature"]["value"] == _sha256(certificate_signing_payload(sealed))


def test_tamper_breaks_signature():
    r = _make_receipt()
    cert = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    # flip a receipted hash on one attestation without re-signing -> verify must fail
    cert["attestations"][0]["receipt_hash"] = "0" * 64
    assert verify_certificate(cert, KEY) is False


def test_receipt_change_moves_certificate():
    # different underlying data (freshness verdict flips STALE) -> different signed cert
    live = build_certificate(_make_receipt(as_of=date(2026, 7, 5)), ORG_A, KEY, issued_at="2026-07-06")
    stale = build_certificate(_make_receipt(as_of=date(2026, 8, 1)), ORG_A, KEY, issued_at="2026-07-06")
    fl = next(a for a in live["attestations"] if a["gate"] == "Freshness verification")
    fs = next(a for a in stale["attestations"] if a["gate"] == "Freshness verification")
    assert fl["state"] == "pass" and fs["state"] == "flag"
    assert live["signature"]["value"] != stale["signature"]["value"]


def test_dnc_and_tcpa_attestations_reflect_the_receipt():
    clean = build_certificate(_make_receipt(dnc=False), ORG_A, KEY, issued_at="2026-07-06")
    dncd = build_certificate(_make_receipt(dnc=True), ORG_A, KEY, issued_at="2026-07-06")
    d_clean = next(a for a in clean["attestations"] if a["gate"] == "DNC scrub status")
    t_clean = next(a for a in clean["attestations"] if a["gate"] == "TCPA gate passage")
    d_dnc = next(a for a in dncd["attestations"] if a["gate"] == "DNC scrub status")
    t_dnc = next(a for a in dncd["attestations"] if a["gate"] == "TCPA gate passage")
    assert d_clean["state"] == "pass" and t_clean["state"] == "pass"
    assert d_dnc["state"] == "flag" and t_dnc["state"] == "flag"
    # clean lead is all-clear; DNC lead is flagged
    assert clean["summary"]["all_clear"] is True
    assert dncd["summary"]["all_clear"] is False and dncd["summary"]["gates_flagged"] >= 2


def test_unknown_dnc_is_never_attested_clear():
    unknown = build_certificate(_make_receipt(dnc=None), ORG_A, KEY, issued_at="2026-07-06")
    dnc_gate = next(a for a in unknown["attestations"] if a["gate"] == "DNC scrub status")
    tcpa_gate = next(a for a in unknown["attestations"] if a["gate"] == "TCPA gate passage")
    assert dnc_gate["state"] == "na"
    assert tcpa_gate["state"] == "na"
    assert unknown["summary"]["all_clear"] is False


def test_no_third_party_pii_in_certificate():
    r = _make_receipt()
    cert = build_certificate(r, ORG_A, KEY, issued_at="2026-07-06")
    blob = canonical_json(cert) + render_certificate_html(cert)
    assert "Jane Q Heir" not in blob     # relative name withheld (only its sha256 lives in the receipt)
    assert "5125551234" not in blob      # phone digits withheld
    assert "min_bid" not in blob         # raw signal payload withheld


def test_batch_rolls_up_under_a_root_hash():
    r1 = _make_receipt(dnc=False, lead_id="aaaaaaaa-0000-0000-0000-000000000001", apn="R1")
    r2 = _make_receipt(dnc=True, lead_id="aaaaaaaa-0000-0000-0000-000000000002", apn="R2")
    batch = build_batch_certificate([r1, r2], ORG_A, KEY, issued_at="2026-07-06")
    assert batch["batch"]["count"] == 2
    # batch root = sha256 over the ordered per-lead chain hashes
    expect_root = _sha256(canonical_json([r1["chain_sha256"], r2["chain_sha256"]]))
    assert batch["batch"]["batch_root_sha256"] == expect_root
    assert verify_certificate(batch, KEY) is True
    # the dirty (DNC) lead is surfaced, not silently averaged away
    assert batch["aggregate"]["leads_all_clear"] is False
    assert r2["lead_id"] in batch["aggregate"]["leads_flagged"]
    # batch HTML is client-clean too
    bh = render_batch_html(batch)
    assert "Provenance-Certified Leads" in bh and "<style" in bh and "APG Land" in bh
    assert "<script" not in bh


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run()
