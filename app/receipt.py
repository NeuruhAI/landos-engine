"""
receipt.py — the AUDITABLE-ACQUISITION lead receipt (tamper-evident provenance).

A lead receipt is a per-lead, point-in-time attestation that answers "why is this a
lead, on what data, scored how, pierced to whom, and is it compliant + still live?"
It is assembled from the real spine — never fabricated — as five sections, each
serialized to canonical JSON and SHA-256'd, with a top-level CHAIN hash over the
section hashes. Same idiom as app/run_manifest.py (hash-the-bytes provenance):
change any section's underlying data and its section hash — and therefore the chain
hash — changes; leave the data alone and the chain is byte-stable across runs.

Sections (fixed order — the chain hashes them in this order):
  SIGNAL      discovery_signals row(s) matched to the lead (source + sha256 of raw_payload)
  SCORING     board score + the composite score row + the scoring_weights version used
  PIERCE      entity -> human chain from contact_relations, with stored confidence
  COMPLIANCE  dnc flag, callable_leads membership, call/skip-trace status
  FRESHNESS   auction/event date vs. as-of date + the freshness-gate verdict

PII doctrine (hard rule): NEVER include third-party PII beyond what the lead row
already carries. So:
  * SIGNAL exposes only a sha256 of raw_payload + non-PII descriptors (never the payload).
  * PIERCE proves the chain with relationship / is_heir / confidence / source and a
    sha256 fingerprint of the relative's name — never the literal name — and phone
    PRESENCE + type, never the digits.
  * COMPLIANCE reports phone PRESENCE + type + DNC state, never the digits.

Pure (DB-free) helpers live up top so the chain-hash behaviour is unit-testable
without a database (mirrors run_manifest.py). One DB assembly function at the bottom
does the reads. A CLI (`python -m app.receipt <APN>`) renders the human receipt.
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .freshness import upcoming_taxsale

# The chain hashes the sections in THIS order. Reordering would change the chain
# hash, so it is fixed and shared by the assembler, the renderer, and the tests.
SECTION_ORDER = ("SIGNAL", "SCORING", "PIERCE", "COMPLIANCE", "FRESHNESS")

RECEIPT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# PURE HELPERS (no DB) — canonical serialization + section/chain hashing.
# ---------------------------------------------------------------------------
def _default(o: Any) -> str:
    """JSON fallback for non-primitive types. Deterministic: the SAME stored value
    always serializes to the SAME string, so the hash is stable across runs.
    Decimal -> str (exact, no float drift), datetime/date -> ISO, UUID -> str."""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    return str(o)


def canonical_json(body: Any) -> str:
    """Deterministic JSON: keys sorted, no incidental whitespace, ASCII-safe."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=_default)


def section_hash(body: Any) -> str:
    """SHA-256 of a section's canonical JSON bytes."""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def sha256_text(s: Optional[str]) -> Optional[str]:
    """SHA-256 fingerprint of a string (used to reference PII without exposing it)."""
    if s is None:
        return None
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def build_chain(sections: Dict[str, Any]) -> Dict[str, Any]:
    """Given {SECTION_NAME: body}, return the receipt spine: each section wrapped with
    its own sha256, plus a top-level chain hash computed over the ordered section
    hashes. Pure — this is the function the tests hammer for stability/sensitivity."""
    wrapped: Dict[str, Any] = {}
    hashes: Dict[str, str] = {}
    for name in SECTION_ORDER:
        body = sections.get(name)
        h = section_hash(body)
        wrapped[name] = {"data": body, "sha256": h}
        hashes[name] = h
    chain = section_hash(
        {"algo": "sha256", "order": list(SECTION_ORDER), "section_hashes": hashes}
    )
    return {"sections": wrapped, "section_hashes": hashes, "chain_sha256": chain}


# ---------------------------------------------------------------------------
# PRESENTATION HELPERS.
# ---------------------------------------------------------------------------
def _has(v: Any) -> bool:
    return v is not None and str(v).strip() != ""


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# SECTION BUILDERS (take already-fetched rows -> section body dict). Kept pure so
# they can be tested with fixtures; the DB reads happen once in assemble_receipt.
# ---------------------------------------------------------------------------
def build_signal_section(signals: List[dict]) -> Dict[str, Any]:
    """discovery_signals matched to the lead. Exposes source descriptors + a sha256
    of each raw_payload (never the payload itself — it may carry PII)."""
    items = []
    for s in signals:
        items.append({
            "source_type": s.get("source_type"),
            "source_ref": s.get("source_ref"),
            "source_url": s.get("source_url"),
            "county_fips": s.get("county_fips"),
            "signal_date": s.get("signal_date"),
            "signal_class": s.get("signal_class"),
            "status": s.get("status"),
            "discovered_at": s.get("ingested_at"),
            "raw_payload_sha256": section_hash(s.get("raw_payload") or {}),
        })
    return {
        "present": bool(items),
        "count": len(items),
        # deterministic order: by (signal_date, source_ref) so the hash is stable
        "signals": sorted(items, key=lambda x: (str(x.get("signal_date") or ""),
                                                str(x.get("source_ref") or ""))),
    }


def build_scoring_section(lead: dict, score_row: Optional[dict],
                          weights_row: Optional[dict]) -> Dict[str, Any]:
    """Board-facing scores off owner_leads + the composite landos.scores row (if the
    lead was /evaluate-scored) + the scoring_weights version that produced it."""
    board = {
        "final_lead_score": _num(lead.get("final_lead_score")),
        "emerging_score": _num(lead.get("emerging_score")),
        "absentee_score": _num(lead.get("absentee_score")),
        "confidence_score": _num(lead.get("confidence_score")),
        "provenance": lead.get("provenance"),
        "source": lead.get("source"),
    }
    composite: Optional[Dict[str, Any]] = None
    if score_row:
        composite = {
            "score": _num(score_row.get("score")),
            "distress_component": _num(score_row.get("distress_component")),
            "owner_component": _num(score_row.get("owner_component")),
            "arbitrage_component": _num(score_row.get("arbitrage_component")),
            "top_factors": score_row.get("top_factors"),
            "weights_version": score_row.get("weights_version"),
            "computed_at": score_row.get("computed_at"),
        }
    weights = None
    if weights_row:
        weights = {
            "version": weights_row.get("version"),
            "profile": weights_row.get("profile"),
            "active": weights_row.get("active"),
            "notes": weights_row.get("notes"),
            "weights": weights_row.get("weights"),
        }
    return {
        "has_composite_score": composite is not None,
        "board_score": board,
        "composite_score": composite,
        "weights_used": weights,
        "note": (None if composite is not None else
                 "No composite landos.scores row — discovered/minted lead "
                 "(board score is emergence-derived, not /evaluate-scored)."),
    }


def build_pierce_section(relations: List[dict]) -> Dict[str, Any]:
    """entity -> human pierce chain from contact_relations. PII-safe: the relative's
    NAME is referenced by sha256 fingerprint only; phones are presence + type, never
    digits. The audit substance (relationship / heirship / confidence / source) is
    what proves the chain and how much to trust it."""
    items = []
    for r in relations:
        items.append({
            "relative_name_sha256": sha256_text(r.get("relative_name")),
            "relationship": r.get("relationship"),
            "is_heir": r.get("is_heir"),
            "confidence": _num(r.get("confidence")),
            "source": r.get("source"),
            "phone_present": _has(r.get("phone")),
            "phone_type": r.get("phone_type"),
            "added_at": r.get("added_at"),
        })
    items.sort(key=lambda x: (-(x["confidence"] or 0), str(x.get("relationship") or "")))
    return {
        "present": bool(items),
        "count": len(items),
        "max_confidence": max((i["confidence"] or 0 for i in items), default=None) if items else None,
        "relations": items,
        "note": (None if items else
                 "No entity->human pierce recorded (not skip-traced / no relatives on file)."),
    }


def build_compliance_section(acq: Optional[dict], lead: dict) -> Dict[str, Any]:
    """DNC state, callable membership (nonblank phone + dnc=false), call + skip-trace
    status. Phone PRESENCE + type only — never the digits."""
    on_board = acq is not None
    dnc = acq.get("dnc") if on_board else None
    phone_1_present = _has((acq or {}).get("phone_1"))
    return {
        "on_board": on_board,  # projected to public.acquisition_leads by sync_to_crm
        "board_status": (acq or {}).get("status"),
        "dnc": dnc,
        # Both facts are required by public.callable_leads; NULL never passes.
        "callable_leads_member": (on_board and phone_1_present and dnc is False),
        "call_status": (acq or {}).get("call_status"),
        "last_call_at": (acq or {}).get("last_call_at"),
        "phone_1_present": phone_1_present,
        "phone_1_type": (acq or {}).get("phone_1_type"),
        "phone_2_present": _has((acq or {}).get("phone_2")),
        "skip_trace_status": lead.get("skip_trace_status"),
        "note": (None if on_board else
                 "Not projected to the board (public.acquisition_leads) — engine-side only."),
    }


def build_freshness_section(signals: List[dict], lead: dict,
                            as_of: date) -> Dict[str, Any]:
    """Auction/event date vs. as-of + the freshness-gate verdict. Point-in-time: the
    verdict is stamped at `as_of` (a receipt attests 'as of DATE this was live')."""
    # Prefer a taxsale signal; else the most-future signal date.
    chosen = None
    for s in signals:
        if str(s.get("source_type") or "").lower().startswith("taxsale") and s.get("signal_date"):
            if chosen is None or (s["signal_date"] > chosen["signal_date"]):
                chosen = s
    if chosen is None:
        dated = [s for s in signals if s.get("signal_date")]
        if dated:
            chosen = max(dated, key=lambda s: s["signal_date"])

    if chosen is None:
        return {
            "gate": "tax-sale freshness",
            "as_of": as_of,
            "event_date": None,
            "ok": None,
            "reason": "no dated signal on this lead — freshness gate not applicable",
            "source_ref": None,
        }
    ok, reason = upcoming_taxsale(
        chosen.get("signal_date"), chosen.get("raw_payload"),
        lead.get("owner_name") or chosen.get("owner_name"), as_of,
    )
    return {
        "gate": "tax-sale freshness",
        "as_of": as_of,
        "event_date": chosen.get("signal_date"),
        "ok": ok,
        "reason": reason,
        "source_ref": chosen.get("source_ref"),
    }


# ---------------------------------------------------------------------------
# DB ASSEMBLY.
# ---------------------------------------------------------------------------
def resolve_owner_lead(cur, lead_id: str) -> Optional[dict]:
    """Accept either an engine owner_leads.lead_id OR a board acquisition_leads.lead_id
    (resolved back to owner_leads via source_lead_id). Returns the owner_leads row."""
    cur.execute("select * from landos.owner_leads where lead_id = %s", (lead_id,))
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        "select source_lead_id from public.acquisition_leads where lead_id = %s",
        (lead_id,),
    )
    board = cur.fetchone()
    if board and board.get("source_lead_id"):
        cur.execute("select * from landos.owner_leads where lead_id = %s",
                    (board["source_lead_id"],))
        return cur.fetchone()
    return None


def resolve_lead_id_by_apn(cur, apn: str) -> List[dict]:
    """CLI helper: owner_leads matching a parcel APN, newest first (APNs are county-
    unique in practice; returns all matches so the caller can disambiguate)."""
    cur.execute(
        "select lead_id, parcel_apn, county_fips, owner_name, created_at "
        "from landos.owner_leads where parcel_apn = %s order by created_at desc",
        (apn,),
    )
    return cur.fetchall()


def assemble_receipt(cur, lead_id: str, as_of: date) -> Optional[Dict[str, Any]]:
    """Read the spine and assemble the full receipt for one lead. Returns None if the
    lead_id resolves to nothing. `as_of` is threaded (not date.today()) so the receipt
    — and the FRESHNESS section hash — is deterministic for a given day (tests fix it)."""
    lead = resolve_owner_lead(cur, lead_id)
    if lead is None:
        return None
    owner_lead_id = lead["lead_id"]
    tenant_id = lead.get("tenant_id")
    apn = lead.get("parcel_apn")

    # SIGNAL — every discovery_signal matched to this lead
    cur.execute(
        "select signal_id, source_type, source_ref, source_url, county_fips, "
        "       signal_date, signal_class, status, ingested_at, raw_payload, owner_name "
        "from landos.discovery_signals where matched_lead_id = %s",
        (owner_lead_id,),
    )
    signals = cur.fetchall()

    # SCORING — latest composite score via identity node (parcel_apn keyed), + weights
    score_row = None
    weights_row = None
    if apn:
        if tenant_id is not None:
            cur.execute(
                "select s.score, s.distress_component, s.owner_component, "
                "       s.arbitrage_component, s.top_factors, s.weights_version, s.computed_at "
                "from landos.identity_nodes n "
                "join landos.scores s on s.identity_node_id = n.id "
                "where n.tenant_id = %s and n.parcel_apn = %s "
                "order by s.computed_at desc limit 1",
                (tenant_id, apn),
            )
        else:
            cur.execute(
                "select s.score, s.distress_component, s.owner_component, "
                "       s.arbitrage_component, s.top_factors, s.weights_version, s.computed_at "
                "from landos.identity_nodes n "
                "join landos.scores s on s.identity_node_id = n.id "
                "where n.parcel_apn = %s order by s.computed_at desc limit 1",
                (apn,),
            )
        score_row = cur.fetchone()
    if score_row and score_row.get("weights_version") is not None:
        if tenant_id is not None:
            cur.execute(
                "select version, profile, active, notes, weights from landos.scoring_weights "
                "where tenant_id = %s and version = %s limit 1",
                (tenant_id, score_row["weights_version"]),
            )
        else:
            cur.execute(
                "select version, profile, active, notes, weights from landos.scoring_weights "
                "where version = %s limit 1",
                (score_row["weights_version"],),
            )
        weights_row = cur.fetchone()

    # PIERCE — entity->human chain
    cur.execute(
        "select relative_name, relationship, phone, phone_type, is_heir, "
        "       confidence, source, added_at "
        "from landos.contact_relations where owner_lead_id = %s",
        (owner_lead_id,),
    )
    relations = cur.fetchall()

    # COMPLIANCE — board projection (acquisition_leads keyed by source_lead_id)
    cur.execute(
        "select lead_id, dnc, call_status, status, phone_1, phone_1_type, phone_2, "
        "       last_call_at from public.acquisition_leads where source_lead_id = %s",
        (owner_lead_id,),
    )
    acq = cur.fetchone()

    sections = {
        "SIGNAL": build_signal_section(signals),
        "SCORING": build_scoring_section(lead, score_row, weights_row),
        "PIERCE": build_pierce_section(relations),
        "COMPLIANCE": build_compliance_section(acq, lead),
        "FRESHNESS": build_freshness_section(signals, lead, as_of),
    }
    spine = build_chain(sections)

    return {
        "receipt_version": RECEIPT_VERSION,
        "generated_by": "landos-engine/lead-receipt",
        "lead_id": str(owner_lead_id),
        "parcel_apn": apn,
        "county_fips": lead.get("county_fips"),
        "owner_name": lead.get("owner_name"),  # already on the lead row (not new PII)
        "situs_address": lead.get("situs_address"),
        "as_of": as_of.isoformat(),
        "hash_algo": "sha256",
        "section_order": list(SECTION_ORDER),
        "section_hashes": spine["section_hashes"],
        "chain_sha256": spine["chain_sha256"],
        "sections": spine["sections"],
    }


# ---------------------------------------------------------------------------
# MARKDOWN RENDER — the human-readable receipt document (?format=md and the CLI).
# ---------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def render_markdown(receipt: Dict[str, Any]) -> str:
    S = {name: receipt["sections"][name]["data"] for name in SECTION_ORDER}
    H = receipt["section_hashes"]
    L: List[str] = []
    L.append(f"# Lead Receipt — {receipt.get('parcel_apn') or receipt['lead_id']}")
    L.append("")
    L.append(f"- **Lead ID:** `{receipt['lead_id']}`")
    L.append(f"- **Parcel APN:** {_fmt(receipt.get('parcel_apn'))}  ·  "
             f"**County FIPS:** {_fmt(receipt.get('county_fips'))}")
    L.append(f"- **Owner:** {_fmt(receipt.get('owner_name'))}")
    L.append(f"- **Situs:** {_fmt(receipt.get('situs_address'))}")
    L.append(f"- **As of:** {receipt['as_of']}  ·  **Receipt v{receipt['receipt_version']}**")
    L.append("")
    L.append(f"### 🔗 CHAIN `sha256:{receipt['chain_sha256']}`")
    L.append("")

    # SIGNAL
    sig = S["SIGNAL"]
    L.append(f"## 1 · SIGNAL  `{H['SIGNAL'][:16]}…`")
    if sig["present"]:
        L.append(f"{sig['count']} matched discovery signal(s):")
        L.append("")
        for s in sig["signals"]:
            L.append(f"- **{_fmt(s['source_type'])}** `{_fmt(s['source_ref'])}` · "
                     f"class={_fmt(s['signal_class'])} · status={_fmt(s['status'])} · "
                     f"event {_fmt(s['signal_date'])} · discovered {_fmt(s['discovered_at'])}")
            L.append(f"  raw_payload sha256 `{s['raw_payload_sha256'][:24]}…`")
    else:
        L.append("No matched discovery signal.")
    L.append("")

    # SCORING
    sc = S["SCORING"]
    b = sc["board_score"]
    L.append(f"## 2 · SCORING  `{H['SCORING'][:16]}…`")
    L.append(f"- Board: final={_fmt(b['final_lead_score'])} · emerging={_fmt(b['emerging_score'])} · "
             f"absentee={_fmt(b['absentee_score'])} · confidence={_fmt(b['confidence_score'])}")
    L.append(f"- Provenance: {_fmt(b['provenance'])} (source {_fmt(b['source'])})")
    if sc["has_composite_score"]:
        c = sc["composite_score"]
        w = sc.get("weights_used") or {}
        L.append(f"- Composite: **{_fmt(c['score'])}** "
                 f"(distress {_fmt(c['distress_component'])} / owner {_fmt(c['owner_component'])} / "
                 f"arbitrage {_fmt(c['arbitrage_component'])})")
        L.append(f"- Weights: version {_fmt(c['weights_version'])} "
                 f"profile={_fmt(w.get('profile'))} active={_fmt(w.get('active'))}")
    else:
        L.append(f"- {sc['note']}")
    L.append("")

    # PIERCE
    p = S["PIERCE"]
    L.append(f"## 3 · PIERCE  `{H['PIERCE'][:16]}…`")
    if p["present"]:
        L.append(f"{p['count']} relation(s), max confidence {_fmt(p['max_confidence'])}:")
        L.append("")
        for r in p["relations"]:
            L.append(f"- {_fmt(r['relationship'])} · heir={_fmt(r['is_heir'])} · "
                     f"confidence={_fmt(r['confidence'])} · source={_fmt(r['source'])} · "
                     f"phone {'present' if r['phone_present'] else 'none'}"
                     f"{(' (' + r['phone_type'] + ')') if r.get('phone_type') else ''}")
            L.append(f"  person ref `{(r['relative_name_sha256'] or '')[:16]}…` (name withheld — PII)")
    else:
        L.append(p["note"])
    L.append("")

    # COMPLIANCE
    cm = S["COMPLIANCE"]
    L.append(f"## 4 · COMPLIANCE  `{H['COMPLIANCE'][:16]}…`")
    L.append(f"- On board: {_fmt(cm['on_board'])} · status={_fmt(cm['board_status'])}")
    L.append(f"- DNC: {_fmt(cm['dnc'])} · callable_leads member: {_fmt(cm['callable_leads_member'])}")
    L.append(f"- Call status: {_fmt(cm['call_status'])} · last call {_fmt(cm['last_call_at'])}")
    L.append(f"- Phones: phone_1 {'present' if cm['phone_1_present'] else 'none'}"
             f"{(' (' + cm['phone_1_type'] + ')') if cm.get('phone_1_type') else ''} · "
             f"phone_2 {'present' if cm['phone_2_present'] else 'none'}")
    L.append(f"- Skip-trace: {_fmt(cm['skip_trace_status'])}")
    if cm.get("note"):
        L.append(f"- {cm['note']}")
    L.append("")

    # FRESHNESS
    f = S["FRESHNESS"]
    verdict = "LIVE" if f["ok"] is True else ("STALE" if f["ok"] is False else "N/A")
    L.append(f"## 5 · FRESHNESS  `{H['FRESHNESS'][:16]}…`")
    L.append(f"- Gate: {_fmt(f['gate'])} · **{verdict}**")
    L.append(f"- Event date: {_fmt(f['event_date'])} · as of {_fmt(f['as_of'])}")
    L.append(f"- Reason: {_fmt(f['reason'])}")
    L.append("")
    L.append("---")
    L.append(f"_Every section is SHA-256'd; the chain hash covers all five in order "
             f"{', '.join(SECTION_ORDER)}. Re-run this receipt on unchanged data and the "
             f"chain hash is identical; change any section and it moves._")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI — `python -m app.receipt <APN> [--json]` — the live demo command.
# ---------------------------------------------------------------------------
def _cli(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    if not args:
        print("usage: python -m app.receipt <APN|lead_id> [--json]", file=sys.stderr)
        return 2
    key = args[0]
    # local imports so `import app.receipt` for tests never touches the DB/env
    from datetime import date as _date
    from .db import pool

    receipt = None
    rc = 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                lead_id = key
                # If it doesn't look like a UUID, treat it as an APN and resolve.
                try:
                    uuid.UUID(str(key))
                    looks_uuid = True
                except (ValueError, AttributeError, TypeError):
                    looks_uuid = False
                if not looks_uuid:
                    matches = resolve_lead_id_by_apn(cur, key)
                    if not matches:
                        print(f"No owner_lead found for APN {key!r}", file=sys.stderr)
                        return 1
                    if len(matches) > 1:
                        print(f"# note: {len(matches)} leads match APN {key!r}; using newest "
                              f"({matches[0]['lead_id']})", file=sys.stderr)
                    lead_id = str(matches[0]["lead_id"])
                receipt = assemble_receipt(cur, lead_id, _date.today())
                if receipt is None:
                    print(f"No lead resolves for {key!r}", file=sys.stderr)
                    rc = 1
    finally:
        # Deterministically shut the pool's worker threads down NOW. Without this the
        # ConnectionPool finalizer tries to join threads at interpreter shutdown, which
        # raises PythonFinalizationError on 3.14 and taints the exit code / stdout.
        pool.close()
    if rc == 0 and receipt is not None:
        if as_json:
            print(json.dumps(receipt, indent=2, default=_default))
        else:
            print(render_markdown(receipt))
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
