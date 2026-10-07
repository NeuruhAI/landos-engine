"""
obituary_correlation.py — P-23: obituary -> estate candidate -> probate correlation. PURE.

THE PIPELINE THIS MODULE IS (no DB, no network, injected inputs only):

    obituary signals ──┐
                       ├─> estate_candidates()   fuzzy identity vs. owner rows
    owner_lead rows ───┘         │                (name-only ⇒ REVIEW candidates)
                                 ▼
    probate signals ────> correlate_probate()    two-key corroboration lifts
                                 │               confidence — disposition UNCHANGED
                                 ▼
                      build_candidate_receipt()  provenance + confidence, hash-chained

MINT SAFETY (the whole point — structural, not policy):
  * disposition is the literal constant DISPOSITION_REVIEW on every candidate this
    module can produce. There is no code path that outputs a mintable disposition —
    a caller cannot flip it by configuration, because no configuration exists.
  * Fuzzy (name-only) confidence is CAPPED at FUZZY_CONFIDENCE_CAP (0.69). Probate
    two-key corroboration may lift it to CORRELATED_CONFIDENCE_CAP (0.85 — the same
    ceiling probate_in itself earns for inventory-backed matches), but the candidate
    remains review-only: the PROBATE signal is the property-linked record; the
    obituary only ever corroborates it.
  * An obituary matching an entity/govt owner is DISCARDED (a person dies; an LLC
    does not). An obituary matching MULTIPLE owners is the common-name problem in
    the flesh: every such candidate is marked ambiguous and penalized.

Determinism: pure string normalization, no external fuzzy libs, sorted outputs,
canonical-JSON receipts (app/receipt idiom) — same inputs, byte-identical receipts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..receipt import canonical_json, section_hash, sha256_text
from ..score_absentee import classify_owner_type

CORRELATION_VERSION = "1.0.0"

DISPOSITION_REVIEW = "review"          # the ONLY disposition this module can emit

FUZZY_CONFIDENCE_CAP = 0.69            # name-only identity NEVER reaches mint-grade
CORRELATED_CONFIDENCE_CAP = 0.85       # probate two-key ceiling (mirrors probate_in)
AMBIGUITY_PENALTY = 0.10               # per extra owner row the same obituary matches

# match-kind base confidences (fuzzy tiers; city corroboration adds CITY_BONUS)
MATCH_BASE = {"exact_tokens": 0.55, "surname_given": 0.50, "surname_initial": 0.30}
CITY_BONUS = 0.10

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "ESQ", "MD", "DDS", "PHD", "REV", "DR"}

# The chain hashes the receipt sections in THIS order (fixed, like app/receipt).
RECEIPT_SECTION_ORDER = ("SOURCE", "IDENTITY", "CORRELATION", "DISPOSITION")


def _name_tokens(name: Optional[str]) -> List[str]:
    """Deterministic identity tokens: uppercase, punctuation stripped, suffixes and
    single-letter initials kept separate from substantive tokens by the callers."""
    toks = re.sub(r"[^A-Z ]", " ", (name or "").upper()).split()
    return [t for t in toks if t not in _SUFFIXES]


def _match_kind(decedent: str, owner: str) -> Optional[str]:
    """The fuzzy identity tiers, strongest first. None = no plausible identity.

    Token conventions differ (obit 'First Middle Last' vs. parcel 'LAST FIRST M') so
    matching is ORDER-FREE on token sets — which is exactly why it stays fuzzy: token
    sets cannot distinguish two people who share a name. That is the review queue's job.
    """
    d, o = _name_tokens(decedent), _name_tokens(owner)
    d_sub = [t for t in d if len(t) > 1]
    o_sub = [t for t in o if len(t) > 1]
    if not d_sub or not o_sub:
        return None
    if sorted(d_sub) == sorted(o_sub):
        return "exact_tokens"
    d_set, o_set = set(d_sub), set(o_sub)
    # surname_given: the decedent's surname (last substantive token) AND given name
    # (first substantive token) both appear among the owner's tokens.
    if d_sub[-1] in o_set and d_sub[0] in o_set:
        return "surname_given"
    # surname_initial: surname matches and the given name matches by first initial.
    if d_sub[-1] in o_set and any(t[0] == d_sub[0][0] for t in o_set - {d_sub[-1]}):
        return "surname_initial"
    return None


def _norm_city(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z ]", "", (s or "").upper()).strip()


def estate_candidates(obit_signals: Iterable[Dict[str, Any]],
                      owner_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fuzzy-identity pass: obituary decedents vs. owner_lead rows.

    obit_signals — normalized adapter output ({source_ref, owner_name, signal_date,
                   raw_payload, ...}); owner_rows — owner_leads projections carrying
                   lead_id, owner_name, parcel_apn, county_fips, situs_city,
                   owner_mailing_city (extra keys ignored).
    Returns REVIEW candidates, sorted (county, obit ref, apn) — deterministic.
    Every candidate: disposition='review', confidence <= FUZZY_CONFIDENCE_CAP.
    """
    owners = [o for o in owner_rows
              if classify_owner_type(o.get("owner_name")) == "individual"]
    candidates: List[Dict[str, Any]] = []
    for obit in sorted(obit_signals, key=lambda s: str(s.get("source_ref") or "")):
        decedent = obit.get("owner_name") or ""
        raw = obit.get("raw_payload") or {}
        obit_city = _norm_city(raw.get("city"))
        hits: List[Tuple[str, bool, Dict[str, Any]]] = []
        for o in owners:
            kind = _match_kind(decedent, o.get("owner_name") or "")
            if kind is None:
                continue
            city_ok = bool(obit_city) and obit_city in {
                _norm_city(o.get("situs_city")), _norm_city(o.get("owner_mailing_city"))}
            hits.append((kind, city_ok, o))
        ambiguous = len(hits) > 1
        for kind, city_ok, o in hits:
            conf = MATCH_BASE[kind] + (CITY_BONUS if city_ok else 0.0)
            conf -= AMBIGUITY_PENALTY * (len(hits) - 1)
            conf = round(max(0.05, min(conf, FUZZY_CONFIDENCE_CAP)), 4)
            candidates.append({
                "kind": "estate_candidate",
                "disposition": DISPOSITION_REVIEW,     # structural: never anything else
                "obit_source_ref": obit.get("source_ref"),
                "obit_signal_date": obit.get("signal_date"),
                "decedent_name": decedent,
                "lead_id": o.get("lead_id"),
                "owner_name": o.get("owner_name"),
                "parcel_apn": o.get("parcel_apn"),
                "county_fips": o.get("county_fips") or obit.get("county_fips"),
                "match_kind": kind,
                "match_keys": ["name"] + (["city"] if city_ok else []),
                "ambiguous": ambiguous,
                "candidate_pool": len(hits),
                "confidence": conf,
                "correlation": None,                   # filled by correlate_probate
            })
    candidates.sort(key=lambda c: (str(c.get("county_fips") or ""),
                                   str(c.get("obit_source_ref") or ""),
                                   str(c.get("parcel_apn") or "")))
    return candidates


def correlate_probate(candidates: List[Dict[str, Any]],
                      probate_signals: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Obituary -> probate correlation. A probate case whose decedent matches the
    candidate's decedent corroborates the identity; if that case is PROPERTY-LINKED
    (parcel_apn or legal_description from the §29-1-12-1 inventory) the identity
    becomes two-key and confidence may rise to CORRELATED_CONFIDENCE_CAP.

    DISPOSITION IS UNCHANGED: still review. The probate signal is the mintable-grade
    record and travels its own pipeline; the obituary candidate only gains context.
    Returns NEW candidate dicts (input not mutated), same deterministic order.
    """
    probates = sorted((p for p in probate_signals),
                      key=lambda p: str(p.get("source_ref") or ""))
    out: List[Dict[str, Any]] = []
    for c in candidates:
        c2 = dict(c)
        for p in probates:
            kind = _match_kind(c["decedent_name"], p.get("owner_name") or "")
            if kind not in ("exact_tokens", "surname_given"):
                continue
            praw = p.get("raw_payload") or {}
            two_key = bool(p.get("parcel_apn") or praw.get("legal_description"))
            apn_agrees = bool(p.get("parcel_apn") and c.get("parcel_apn")
                              and str(p["parcel_apn"]) == str(c["parcel_apn"]))
            c2["correlation"] = {
                "probate_case": p.get("source_ref"),
                "probate_signal_date": p.get("signal_date"),
                "two_key": two_key,
                "apn_agrees": apn_agrees,
                "match_kind": kind,
            }
            if two_key and apn_agrees:
                # the estate inventory names THIS parcel — the strongest corroboration
                c2["confidence"] = round(min(CORRELATED_CONFIDENCE_CAP,
                                             c["confidence"] + 0.30), 4)
            elif two_key:
                c2["confidence"] = round(min(CORRELATED_CONFIDENCE_CAP,
                                             c["confidence"] + 0.15), 4)
            else:
                # a name-only probate docket match is MORE fuzz, not a second key:
                # confidence stays inside the fuzzy cap
                c2["confidence"] = round(min(FUZZY_CONFIDENCE_CAP,
                                             c["confidence"] + 0.05), 4)
            break   # first matching case (sorted order) — deterministic
        c2["disposition"] = DISPOSITION_REVIEW   # structural re-assert, never mintable
        out.append(c2)
    return out


def build_candidate_receipt(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """The provenance + confidence receipt for ONE review candidate (hash-chained,
    app/receipt idiom). PII doctrine: the decedent is third-party PII — the receipt
    carries a sha256 fingerprint, never the literal name; surviving family never
    appears at all (the SIGNAL raw_payload keeps it; receipts don't). The owner name
    is already on the lead row, so referencing it adds no new PII exposure."""
    sections = {
        "SOURCE": {
            "source_type": "obituary",
            "source_ref": candidate.get("obit_source_ref"),
            "signal_date": candidate.get("obit_signal_date"),
            "county_fips": candidate.get("county_fips"),
            "decedent_sha256": sha256_text(candidate.get("decedent_name")),
            "name_only": True,
        },
        "IDENTITY": {
            "lead_id": str(candidate.get("lead_id")) if candidate.get("lead_id") else None,
            "parcel_apn": candidate.get("parcel_apn"),
            "owner_name": candidate.get("owner_name"),
            "match_kind": candidate.get("match_kind"),
            "match_keys": candidate.get("match_keys"),
            "ambiguous": candidate.get("ambiguous"),
            "candidate_pool": candidate.get("candidate_pool"),
        },
        "CORRELATION": candidate.get("correlation") or {"probate_case": None,
                                                        "two_key": False},
        "DISPOSITION": {
            "disposition": candidate.get("disposition"),
            "confidence": candidate.get("confidence"),
            "fuzzy_cap": FUZZY_CONFIDENCE_CAP,
            "correlated_cap": CORRELATED_CONFIDENCE_CAP,
            "mint_eligible": False,   # constant by construction — see module docstring
            "reason": "fuzzy identity can only produce a review candidate; "
                      "no automatic lead mint on obituary matching",
        },
    }
    hashes = {name: section_hash(sections[name]) for name in RECEIPT_SECTION_ORDER}
    chain = section_hash({"algo": "sha256", "order": list(RECEIPT_SECTION_ORDER),
                          "section_hashes": hashes})
    return {
        "receipt_kind": "obituary_estate_candidate",
        "correlation_version": CORRELATION_VERSION,
        "sections": {n: {"data": sections[n], "sha256": hashes[n]}
                     for n in RECEIPT_SECTION_ORDER},
        "section_hashes": hashes,
        "chain_sha256": chain,
    }


def receipt_canonical(receipt: Dict[str, Any]) -> str:
    return canonical_json(receipt)
