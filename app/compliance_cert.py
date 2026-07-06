"""
compliance_cert.py — the COMPLIANCE CERTIFICATE GENERATOR (a sellable diligence artifact).

WHAT THIS IS
------------
The estate already emits a tamper-evident, hash-chained lead receipt (app/receipt.py):
five sections — SIGNAL / SCORING / PIERCE / COMPLIANCE / FRESHNESS — each SHA-256'd under
one chain hash. That receipt is engineer-facing and PII-safe but not something you hand a
buyer. This module PRODUCTIZES it: it turns a receipt (one lead, or a batch) into a clean,
client-facing **Compliance Certificate** — a signed attestation that a BUYER of the lead
can independently verify.

Each certificate line is an ATTESTATION backed by a real receipt hash:
  * Signal source + date        <- SIGNAL   section hash
  * DNC scrub status + as-of    <- COMPLIANCE section hash
  * TCPA gate passage           <- COMPLIANCE section hash
  * Data provenance             <- the top-level CHAIN hash (binds all five sections)
  * Freshness verification      <- FRESHNESS section hash

SOC2-ADJACENT FRAMING
---------------------
The document asserts: "this lead passed [N] compliance gates; every assertion is
cryptographically receipted (SHA-256) against the lead's provenance chain and is
independently verifiable by re-deriving the receipt from source." That is a diligence
claim, NOT a framework certification — it says exactly what the receipt proves and nothing
more (same honesty doctrine as the lead receipt: never assert what the spine can't back).

WHITE-LABEL / RESALE DOCTRINE
-----------------------------
Zero hardcoded branding. The issuer org name / url / tagline are passed in (the HTTP + CLI
layers read them from CERT_ORG_NAME / CERT_ORG_URL / CERT_ORG_TAGLINE, defaulting to a
neutral engine name). A reseller white-labels a batch by setting one env var. The pure
generators take an `org` dict so a tenant's name never has to be compiled in.

SIGNATURE
---------
The certificate is signed over its canonical body (everything except the signature):
  * with CERT_SIGNING_KEY set  -> HMAC-SHA256(key, canonical_body)  ("hmac-sha256")
  * without a key              -> SHA-256(canonical_body)           ("sha256-content")
Either way `verify_certificate()` recomputes it. The HMAC path is a real signature a buyer
who holds the shared key can check; the content-hash path is a tamper-evident self-seal.
This mirrors the estate's crypto idiom (hash-the-bytes provenance), not a new PKI.

═══════════════════════════════════════════════════════════════════════════════════════════
MONETIZATION (name the path; billing is out of scope here)
═══════════════════════════════════════════════════════════════════════════════════════════
Two revenue paths ride on this generator, sold on top of the existing List Desk product:

  1. PROVENANCE-CERTIFIED LEADS — a premium tier on List Desk. The same lead list ships at
     a markup when each row is accompanied by a Compliance Certificate a buyer can verify
     (signal provenance + DNC/TCPA gate + freshness, cryptographically receipted). "Raw
     lead" vs "certified lead" becomes a price ladder: certification is the up-sell, and it
     costs ~nothing to produce because the receipt already exists. A batch certificate is
     the diligence packet that justifies the higher per-lead price to a wholesaler/hedge
     buyer who would otherwise discount for provenance risk.

  2. STANDALONE COMPLIANCE-AUDIT SERVICE — sell the certificate itself, not the lead. A
     tenant (or an outside list broker) pulls certs over the API (compliance-cert/{lead_id}
     or /compliance-cert/batch) for leads they already hold, paying per certificate or on a
     subscription. This is a SOC2-adjacent, buyer-facing "prove your list is clean" audit
     rail — the compliance layer sold AS a product, independent of lead sales.

Pricing is set by the tenant; this module is the deterministic, $0-marginal-cost engine
underneath both. Billing/metering is deliberately NOT built here.
═══════════════════════════════════════════════════════════════════════════════════════════

Pure (DB-free) generators up top so behaviour is unit-testable with fixtures; the DB reads
happen in app/receipt.assemble_receipt (reused verbatim). A CLI renders the document.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import sys
from typing import Any, Dict, List, Optional

from .receipt import canonical_json, SECTION_ORDER

CERT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# SIGNATURE HELPERS.
# ---------------------------------------------------------------------------
def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def certificate_signing_payload(cert: Dict[str, Any]) -> str:
    """The exact bytes a signature covers: the canonical certificate body with the
    `signature` field removed. Signer and verifier MUST use this identical function."""
    body = {k: v for k, v in cert.items() if k != "signature"}
    return canonical_json(body)


def _sign(payload: str, signing_key: Optional[str]) -> Dict[str, str]:
    if signing_key:
        value = hmac.new(signing_key.encode("utf-8"), payload.encode("utf-8"),
                         hashlib.sha256).hexdigest()
        return {"algo": "hmac-sha256", "value": value,
                "signed_over": "canonical certificate body excluding signature"}
    return {"algo": "sha256-content", "value": _sha256(payload),
            "signed_over": "canonical certificate body excluding signature"}


def verify_certificate(cert: Dict[str, Any], signing_key: Optional[str] = None) -> bool:
    """Recompute the signature over the canonical body and compare (constant-time)."""
    sig = cert.get("signature") or {}
    expect = _sign(certificate_signing_payload(cert), signing_key)
    if sig.get("algo") != expect["algo"]:
        return False
    return hmac.compare_digest(str(sig.get("value", "")), expect["value"])


# ---------------------------------------------------------------------------
# ATTESTATION BUILDERS — each maps one receipt section to a client-facing line,
# backed by that section's real SHA-256 (or the chain hash). Pure functions of the
# already-assembled receipt; they NEVER invent a hash or a claim the receipt can't back.
# ---------------------------------------------------------------------------
def _section(receipt: Dict[str, Any], name: str) -> Dict[str, Any]:
    return (receipt.get("sections", {}).get(name, {}) or {}).get("data", {}) or {}


def _hash(receipt: Dict[str, Any], name: str) -> Optional[str]:
    return (receipt.get("section_hashes") or {}).get(name)


def _attest_signal(receipt: Dict[str, Any]) -> Dict[str, Any]:
    data = _section(receipt, "SIGNAL")
    signals = data.get("signals") or []
    present = bool(data.get("present"))
    sources = sorted({s.get("source_type") for s in signals if s.get("source_type")})
    dates = sorted([str(s.get("signal_date")) for s in signals if s.get("signal_date")])
    span = (f"{dates[0]}" + (f" … {dates[-1]}" if dates[-1] != dates[0] else "")) if dates else "—"
    stmt = (f"Lead originates from {data.get('count', 0)} verified discovery signal(s) "
            f"[{', '.join(sources) or 'unclassified'}]; event date(s) {span}. Each source's "
            f"raw payload is SHA-256 fingerprinted in the receipt (payload withheld — PII)."
            if present else
            "No discovery signal is matched to this lead — signal provenance cannot be attested.")
    return {"gate": "Signal source & date", "statement": stmt,
            "state": "pass" if present else "na",
            "receipt_section": "SIGNAL", "receipt_hash": _hash(receipt, "SIGNAL"),
            "evidence": {"source_types": sources, "event_date_span": span,
                         "signal_count": data.get("count", 0)}}


def _attest_dnc(receipt: Dict[str, Any]) -> Dict[str, Any]:
    data = _section(receipt, "COMPLIANCE")
    dnc = data.get("dnc")
    as_of = receipt.get("as_of")
    if dnc is False:
        state, verdict = "pass", "cleared (dnc=false)"
    elif dnc is True:
        state, verdict = "flag", "BLOCKED — on the Do-Not-Call registry (dnc=true)"
    else:
        state, verdict = "na", "not yet scrubbed / not projected to the callable board"
    stmt = (f"DNC scrub verified as of {as_of}: contact is {verdict}. "
            f"Last call: {data.get('last_call_at') or 'none on file'}.")
    return {"gate": "DNC scrub status", "statement": stmt, "state": state,
            "receipt_section": "COMPLIANCE", "receipt_hash": _hash(receipt, "COMPLIANCE"),
            "evidence": {"dnc": dnc, "as_of": as_of,
                         "last_call_at": data.get("last_call_at")}}


def _attest_tcpa(receipt: Dict[str, Any]) -> Dict[str, Any]:
    data = _section(receipt, "COMPLIANCE")
    callable_member = data.get("callable_leads_member")
    dnc = data.get("dnc")
    if callable_member is True:
        state, verdict = "pass", "PASSED — contact is on the callable list; outreach permitted"
    elif dnc is True:
        state, verdict = "flag", "WITHHELD — DNC-blocked; automated outreach must be suppressed"
    else:
        state, verdict = "na", "UNVERIFIED — not projected to the callable board"
    stmt = (f"TCPA gate {verdict} (callable_leads_member="
            f"{'true' if callable_member else 'false' if callable_member is False else 'unknown'}). "
            f"The callable list is defined as dnc=false, so membership IS the gate.")
    return {"gate": "TCPA gate passage", "statement": stmt, "state": state,
            "receipt_section": "COMPLIANCE", "receipt_hash": _hash(receipt, "COMPLIANCE"),
            "evidence": {"callable_leads_member": callable_member, "dnc": dnc}}


def _attest_provenance(receipt: Dict[str, Any]) -> Dict[str, Any]:
    scoring = _section(receipt, "SCORING")
    pierce = _section(receipt, "PIERCE")
    board = scoring.get("board_score") or {}
    comp = scoring.get("composite_score") or {}
    prov = board.get("provenance")
    wv = comp.get("weights_version")
    stmt = (f"Provenance chain bound under receipt chain hash {(receipt.get('chain_sha256') or '')[:16]}…: "
            f"lead provenance '{prov or 'n/a'}'"
            + (f", composite score (weights v{wv})" if scoring.get('has_composite_score') else
               ", board/emergence score")
            + f"; entity→human pierce = {pierce.get('count', 0)} relation(s)"
            + (f", max confidence {pierce.get('max_confidence')}" if pierce.get('present') else "")
            + ". Every section is SHA-256'd; the chain hash binds all five in order "
            + ", ".join(SECTION_ORDER) + ".")
    return {"gate": "Data provenance", "statement": stmt, "state": "pass",
            "receipt_section": "CHAIN", "receipt_hash": receipt.get("chain_sha256"),
            "evidence": {"provenance": prov, "weights_version": wv,
                         "pierce_count": pierce.get("count", 0),
                         "section_hashes": receipt.get("section_hashes")}}


def _attest_freshness(receipt: Dict[str, Any]) -> Dict[str, Any]:
    data = _section(receipt, "FRESHNESS")
    ok = data.get("ok")
    if ok is True:
        state, verdict = "pass", "LIVE"
    elif ok is False:
        state, verdict = "flag", "STALE"
    else:
        state, verdict = "na", "NOT DATE-BOUND"
    stmt = (f"Freshness gate '{data.get('gate')}' = {verdict} as of {data.get('as_of')}: "
            f"underlying event {data.get('event_date') or '—'}. {data.get('reason') or ''}").strip()
    return {"gate": "Freshness verification", "statement": stmt, "state": state,
            "receipt_section": "FRESHNESS", "receipt_hash": _hash(receipt, "FRESHNESS"),
            "evidence": {"ok": ok, "event_date": data.get("event_date"),
                         "as_of": data.get("as_of")}}


# The fixed attestation order = the compliance narrative a buyer reads top-to-bottom.
ATTESTORS = (_attest_signal, _attest_dnc, _attest_tcpa, _attest_provenance, _attest_freshness)


def _summarize(attestations: List[Dict[str, Any]]) -> Dict[str, Any]:
    passed = [a for a in attestations if a["state"] == "pass"]
    flagged = [a for a in attestations if a["state"] == "flag"]
    na = [a for a in attestations if a["state"] == "na"]
    applicable = len(attestations) - len(na)
    return {"gates_total": len(attestations), "gates_applicable": applicable,
            "gates_passed": len(passed), "gates_flagged": len(flagged),
            "gates_not_applicable": len(na),
            "all_clear": len(flagged) == 0 and len(passed) > 0,
            "all_verifiable": all(a.get("receipt_hash") for a in attestations)}


# ---------------------------------------------------------------------------
# CERTIFICATE BUILDERS (pure).
# ---------------------------------------------------------------------------
def build_certificate(receipt: Dict[str, Any], org: Dict[str, Any],
                      signing_key: Optional[str] = None,
                      issued_at: Optional[str] = None) -> Dict[str, Any]:
    """Turn ONE lead receipt into a signed Compliance Certificate. `issued_at` is threaded
    (not now()) so a certificate is byte-deterministic for a given issuance — tests fix it,
    and the same lead+data+org+issued_at always yields the same signature."""
    attestations = [fn(receipt) for fn in ATTESTORS]
    summary = _summarize(attestations)
    chain = receipt.get("chain_sha256") or ""
    cert = {
        "schema": "compliance-certificate@1.0",
        "cert_version": CERT_VERSION,
        "cert_id": "CC-" + (chain[:12] or "00000000"),
        "issuer": {
            "org_name": org.get("org_name") or "LandOS",
            "org_url": org.get("org_url"),
            "tagline": org.get("tagline"),
        },
        "subject": {
            "lead_id": receipt.get("lead_id"),
            "parcel_apn": receipt.get("parcel_apn"),
            "county_fips": receipt.get("county_fips"),
            "situs_address": receipt.get("situs_address"),
            "as_of": receipt.get("as_of"),
        },
        "receipt_ref": {
            "receipt_version": receipt.get("receipt_version"),
            "hash_algo": receipt.get("hash_algo") or "sha256",
            "chain_sha256": chain,
            "section_hashes": receipt.get("section_hashes"),
        },
        "attestations": attestations,
        "summary": summary,
        "soc2_statement": (
            f"This lead passed {summary['gates_passed']} of {summary['gates_applicable']} "
            f"applicable compliance gate(s). Every assertion above is cryptographically "
            f"receipted (SHA-256) against the lead's provenance chain "
            f"{chain[:16]}… and is independently verifiable by re-deriving the receipt from "
            f"source. This attests the audit chain only — it is not a framework certification."
        ),
        "issued_at": issued_at,
    }
    cert["signature"] = _sign(certificate_signing_payload(cert), signing_key)
    return cert


def build_batch_certificate(receipts: List[Dict[str, Any]], org: Dict[str, Any],
                            signing_key: Optional[str] = None,
                            issued_at: Optional[str] = None) -> Dict[str, Any]:
    """Certify a BATCH: one certificate per lead, rolled up under a batch-root hash =
    SHA-256 over the ordered per-lead chain hashes (a change to any lead's provenance moves
    the root). This is the diligence packet that justifies a certified-list markup."""
    certs = [build_certificate(r, org, signing_key, issued_at) for r in receipts]
    chain_hashes = [c["receipt_ref"]["chain_sha256"] for c in certs]
    batch_root = _sha256(canonical_json(chain_hashes))
    leads = [{
        "lead_id": c["subject"]["lead_id"],
        "parcel_apn": c["subject"]["parcel_apn"],
        "cert_id": c["cert_id"],
        "chain_sha256": c["receipt_ref"]["chain_sha256"],
        "summary": c["summary"],
    } for c in certs]
    agg = {
        "count": len(certs),
        "gates_passed": sum(c["summary"]["gates_passed"] for c in certs),
        "gates_flagged": sum(c["summary"]["gates_flagged"] for c in certs),
        "leads_all_clear": all(c["summary"]["all_clear"] for c in certs) and bool(certs),
        "leads_flagged": [c["subject"]["lead_id"] for c in certs
                          if c["summary"]["gates_flagged"] > 0],
    }
    gates_per_lead = certs[0]["summary"]["gates_total"] if certs else 0
    cert = {
        "schema": "compliance-certificate-batch@1.0",
        "cert_version": CERT_VERSION,
        "cert_id": "CCB-" + (batch_root[:12] or "00000000"),
        "issuer": {
            "org_name": org.get("org_name") or "LandOS",
            "org_url": org.get("org_url"),
            "tagline": org.get("tagline"),
        },
        "batch": {"count": len(certs), "batch_root_sha256": batch_root, "leads": leads},
        "aggregate": agg,
        "soc2_statement": (
            f"Every one of the {len(certs)} lead(s) in this batch was run through "
            f"{gates_per_lead} compliance gate(s); {agg['gates_passed']} gate-pass "
            f"attestation(s) in total, each cryptographically receipted (SHA-256) and "
            f"independently verifiable. Batch integrity is bound under root hash "
            f"{batch_root[:16]}…. "
            + ("All leads are clear of DNC/TCPA/freshness flags."
               if agg["leads_all_clear"] else
               f"{len(agg['leads_flagged'])} lead(s) carry a flag — see the per-lead rows.")
        ),
        "issued_at": issued_at,
        "certificates": certs,
    }
    cert["signature"] = _sign(certificate_signing_payload(cert), signing_key)
    return cert


# ---------------------------------------------------------------------------
# HTML RENDER — client-facing, letterhead-ready, light theme, print-friendly,
# fully self-contained (inline CSS). Deliberately NOT the terminal-dark engine
# aesthetic: this is the document a buyer's counsel reads or files.
# ---------------------------------------------------------------------------
def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


_BADGE = {"pass": ("PASS", "#0f7b3f", "#e6f4ec"),
          "flag": ("REVIEW", "#8a5a00", "#fdf3e0"),
          "na": ("N/A", "#5b6470", "#eef0f2")}

_CSS = """
:root{--ink:#14181f;--muted:#5b6470;--line:#e2e6ea;--accent:#3D5A6C;--bg:#ffffff;--panel:#f7f9fb}
*{box-sizing:border-box}
body{margin:0;background:#eef1f4;color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
 font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.sheet{max-width:820px;margin:28px auto;background:var(--bg);border:1px solid var(--line);
 border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(20,24,31,.06)}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
 padding:26px 32px;border-bottom:3px solid var(--accent)}
.brand{font-size:20px;font-weight:700;letter-spacing:.2px;color:var(--accent)}
.brand .tag{display:block;font-size:12px;font-weight:500;color:var(--muted);letter-spacing:.3px;margin-top:2px}
.doc{text-align:right}
.doc .kind{font-size:12px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted)}
.doc .title{font-size:19px;font-weight:700;margin-top:2px}
.doc .cid{font-size:12px;color:var(--muted);margin-top:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.body{padding:24px 32px 30px}
.subject{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;background:var(--panel);
 border:1px solid var(--line);border-radius:8px;padding:16px 20px;margin-bottom:22px}
.subject div{font-size:13px}
.subject .k{color:var(--muted);text-transform:uppercase;letter-spacing:.6px;font-size:11px}
.subject .v{font-weight:600;word-break:break-word}
.soc2{border-left:4px solid var(--accent);background:#f2f6f9;padding:14px 18px;border-radius:0 8px 8px 0;
 font-size:14px;color:#243744;margin-bottom:24px}
.soc2 b{color:var(--accent)}
h2.section{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);
 margin:0 0 12px;font-weight:700}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;vertical-align:top;padding:12px 10px;border-bottom:1px solid var(--line);font-size:13.5px}
th{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);border-bottom:2px solid var(--line)}
td.gate{font-weight:600;width:150px}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 9px;border-radius:999px;white-space:nowrap}
.hash{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#33506a;word-break:break-all}
.hash .lbl{color:var(--muted)}
.foot{margin-top:24px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
.foot .sig{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#33506a;word-break:break-all}
.verify{background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:12px 16px;margin-top:14px;font-size:12.5px}
@media print{body{background:#fff}.sheet{border:none;box-shadow:none;margin:0;max-width:none}}
@media (max-width:640px){.subject{grid-template-columns:1fr}.head{flex-direction:column}.doc{text-align:left}}
"""


def _badge_html(state: str) -> str:
    label, fg, bg = _BADGE.get(state, _BADGE["na"])
    return (f'<span class="badge" style="color:{fg};background:{bg}">{label}</span>')


def _shell(title: str, inner: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>{inner}</body></html>")


def _letterhead(issuer: Dict[str, Any], kind: str, title: str, cid: str) -> str:
    tag = issuer.get("tagline")
    url = issuer.get("org_url")
    brand = _e(issuer.get("org_name") or "LandOS")
    if url:
        brand = f'<a href="{_e(url)}" style="color:inherit;text-decoration:none">{brand}</a>'
    tag_html = f'<span class="tag">{_e(tag)}</span>' if tag else ""
    return (f'<div class="head"><div class="brand">{brand}{tag_html}</div>'
            f'<div class="doc"><div class="kind">{_e(kind)}</div>'
            f'<div class="title">{_e(title)}</div><div class="cid">{_e(cid)}</div></div></div>')


def render_certificate_html(cert: Dict[str, Any]) -> str:
    s = cert["subject"]
    rows = []
    for a in cert["attestations"]:
        h = a.get("receipt_hash") or "—"
        rows.append(
            f'<tr><td class="gate">{_e(a["gate"])}</td>'
            f'<td>{_e(a["statement"])}</td>'
            f'<td>{_badge_html(a["state"])}</td>'
            f'<td class="hash"><span class="lbl">{_e(a["receipt_section"])} · sha256</span><br>{_e(h)}</td></tr>'
        )
    subj = "".join(
        f'<div><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div></div>'
        for k, v in [("Lead ID", s.get("lead_id")), ("Parcel APN", s.get("parcel_apn")),
                     ("County FIPS", s.get("county_fips")), ("As of", s.get("as_of")),
                     ("Situs", s.get("situs_address"))] if v is not None
    )
    sm = cert["summary"]
    sig = cert["signature"]
    inner = (
        f'<div class="sheet">'
        + _letterhead(cert["issuer"], "Compliance Certificate", "Auditable Acquisition", cert["cert_id"])
        + '<div class="body">'
        + f'<div class="subject">{subj}</div>'
        + f'<div class="soc2">{_e(cert["soc2_statement"])}</div>'
        + '<h2 class="section">Attestations — each line is receipted &amp; independently verifiable</h2>'
        + '<table><thead><tr><th>Gate</th><th>Attestation</th><th>Status</th><th>Receipt hash</th></tr></thead>'
        + f'<tbody>{"".join(rows)}</tbody></table>'
        + f'<div class="verify"><b>How to verify:</b> re-run the lead receipt at its source '
        + f'(<code>GET /lead-receipt/{_e(s.get("lead_id"))}</code>), recompute each section SHA-256 and the '
        + f'chain hash, and confirm they match the hashes above and the signature below. '
        + f'{_e(sm["gates_passed"])}/{_e(sm["gates_applicable"])} applicable gate(s) passed; '
        + f'{_e(sm["gates_flagged"])} flagged for review.</div>'
        + f'<div class="foot">Provenance chain '
        + f'<span class="sig">{_e(cert["receipt_ref"]["chain_sha256"])}</span><br>'
        + f'Signature ({_e(sig["algo"])}) <span class="sig">{_e(sig["value"])}</span>'
        + (f'<br>Issued {_e(cert["issued_at"])}' if cert.get("issued_at") else "")
        + '</div></div></div>'
    )
    return _shell(f'Compliance Certificate {cert["cert_id"]}', inner)


def render_batch_html(cert: Dict[str, Any]) -> str:
    b = cert["batch"]
    rows = []
    for lead in b["leads"]:
        sm = lead["summary"]
        state = "flag" if sm["gates_flagged"] else ("pass" if sm["all_clear"] else "na")
        rows.append(
            f'<tr><td class="gate">{_e(lead.get("parcel_apn") or lead.get("lead_id"))}</td>'
            f'<td>{_e(sm["gates_passed"])}/{_e(sm["gates_applicable"])} passed'
            + (f', <b>{_e(sm["gates_flagged"])} flagged</b>' if sm["gates_flagged"] else "")
            + f'</td><td>{_badge_html(state)}</td>'
            f'<td class="hash">{_e(lead["chain_sha256"])}</td></tr>'
        )
    sig = cert["signature"]
    inner = (
        f'<div class="sheet">'
        + _letterhead(cert["issuer"], "Batch Compliance Certificate",
                      f'{b["count"]} Provenance-Certified Leads', cert["cert_id"])
        + '<div class="body">'
        + f'<div class="soc2">{_e(cert["soc2_statement"])}</div>'
        + '<h2 class="section">Per-lead attestation summary</h2>'
        + '<table><thead><tr><th>Lead</th><th>Gates</th><th>Status</th><th>Provenance chain hash</th></tr></thead>'
        + f'<tbody>{"".join(rows)}</tbody></table>'
        + f'<div class="foot">Batch root '
        + f'<span class="sig">{_e(b["batch_root_sha256"])}</span><br>'
        + f'Signature ({_e(sig["algo"])}) <span class="sig">{_e(sig["value"])}</span>'
        + (f'<br>Issued {_e(cert["issued_at"])}' if cert.get("issued_at") else "")
        + '</div></div></div>'
    )
    return _shell(f'Batch Compliance Certificate {cert["cert_id"]}', inner)


# ---------------------------------------------------------------------------
# ENV CONFIG (white-label) — read by the HTTP + CLI layers, never by the pure
# generators. No hardcoded brand: everything is overridable.
# ---------------------------------------------------------------------------
def org_from_env() -> Dict[str, Any]:
    import os
    return {
        "org_name": os.environ.get("CERT_ORG_NAME") or os.environ.get("LANDOS_ORG_NAME") or "LandOS",
        "org_url": os.environ.get("CERT_ORG_URL") or None,
        "tagline": os.environ.get("CERT_ORG_TAGLINE") or None,
    }


def signing_key_from_env() -> Optional[str]:
    import os
    return os.environ.get("CERT_SIGNING_KEY") or None


# ---------------------------------------------------------------------------
# CLI — `python -m app.compliance_cert <APN|lead_id> [--html|--json]`.
# Mirrors app.receipt's CLI: resolve -> assemble receipt -> build+render cert.
# ---------------------------------------------------------------------------
def _cli(argv: List[str]) -> int:
    import json
    import uuid
    from datetime import date

    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    if not args:
        print("usage: python -m app.compliance_cert <APN|lead_id> [--html|--json]", file=sys.stderr)
        return 2

    from .db import pool
    from . import receipt as receipt_mod

    org = org_from_env()
    key = signing_key_from_env()
    issued_at = date.today().isoformat()

    cert = None
    rc = 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                lead_key = args[0]
                try:
                    uuid.UUID(str(lead_key))
                    looks_uuid = True
                except (ValueError, AttributeError, TypeError):
                    looks_uuid = False
                if not looks_uuid:
                    matches = receipt_mod.resolve_lead_id_by_apn(cur, lead_key)
                    if not matches:
                        print(f"No owner_lead found for APN {lead_key!r}", file=sys.stderr)
                        return 1
                    lead_key = str(matches[0]["lead_id"])
                receipt = receipt_mod.assemble_receipt(cur, lead_key, date.today())
                if receipt is None:
                    print(f"No lead resolves for {args[0]!r}", file=sys.stderr)
                    rc = 1
                else:
                    cert = build_certificate(receipt, org, key, issued_at)
    finally:
        pool.close()

    if rc == 0 and cert is not None:
        print(json.dumps(cert, indent=2) if as_json else render_certificate_html(cert))
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
