"""
taxsale_predicate.py — G1: the COMPLETE tax-sale promotion predicate, pure.

One function answers "may this taxsale signal mint?" for every caller — the minter
(defense-in-depth on top of the gate-in-DATA SQL), the dry-run reconciler, and the
tests. No DB, no network, no clock reads: `today` is injected (single clock per run)
so the same rows always produce the same verdicts.

THE PREDICATE (all four, in precedence order for reporting):
  flagged          diligence_flag is set — a human found a problem. TERMINAL: the
                   record never mints, whatever else is true, until a human clears it.
  stale            the freshness gate (app/freshness.upcoming_taxsale) rejected it —
                   a past/resolved sale is a consumed signal, dead regardless of review.
  needs_diligence  the diligence review has not been completed (fail-closed default).
  not_promoted     no human keystroke yet (mig 036).
A row failing several checks reports the highest-precedence reason — deterministic,
never dependent on evaluation order.

RECONCILIATION: reconcile() classifies EVERY taxsale signal it is handed and returns a
county-by-county breakdown (eligible + excluded-by-reason) plus the invariant proof
that the books balance (total == eligible + sum(excluded)). The output is canonically
ordered and hashed (app/receipt idiom) so two dry-runs over the same rows — in any
input order — produce byte-identical receipts. That hash IS the dry-run reconciliation:
if it moves between runs, the underlying data moved.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

from .freshness import upcoming_taxsale
from .receipt import canonical_json, section_hash

PREDICATE_VERSION = "1.0.0"

# Exclusion reason codes, highest precedence first. `eligible` is not an exclusion.
EXCLUSION_ORDER: Tuple[str, ...] = ("flagged", "stale", "needs_diligence", "not_promoted")


def evaluate_mint_predicate(signal: Dict[str, Any], today: date) -> Tuple[bool, str, str]:
    """(eligible, reason_code, reason) for ONE taxsale signal row.

    `signal` carries the discovery_signals fields: promoted, needs_diligence,
    diligence_flag, signal_date, raw_payload, owner_name. Missing keys read as the
    FAIL-CLOSED value (unpromoted, needs diligence) — a partial row never mints.
    """
    flag = signal.get("diligence_flag")
    if flag is not None and str(flag).strip() != "":
        return (False, "flagged", f"diligence flag set: {str(flag).strip()!r} — never mints")

    ok, why = upcoming_taxsale(signal.get("signal_date"), signal.get("raw_payload"),
                               signal.get("owner_name"), today)
    if not ok:
        return (False, "stale", f"freshness gate: {why}")

    # fail-closed: an absent needs_diligence reads as TRUE (review not done)
    if bool(signal.get("needs_diligence", True)):
        return (False, "needs_diligence", "diligence review not completed (fail-closed)")

    if not bool(signal.get("promoted", False)):
        return (False, "not_promoted", "no human promote keystroke (mig 036)")

    return (True, "eligible", f"fresh + promoted + diligence clear ({why})")


def reconcile(signals: Iterable[Dict[str, Any]], today: date) -> Dict[str, Any]:
    """Deterministic dry-run reconciliation over a full set of taxsale signal rows.

    Returns:
      {
        as_of, predicate_version, total, eligible,
        excluded: {reason: n},                       # every reason key always present
        per_county: {fips: {total, eligible, excluded: {reason: n}}},
        eligible_refs: [source_ref...],              # sorted — the would-mint list
        reconciled: bool,                            # total == eligible + sum(excluded)
        receipt_sha256: <hash of everything above>   # byte-stable across input order
      }
    Input order never matters: counts are order-free and every list is sorted.
    """
    per_county: Dict[str, Dict[str, Any]] = {}
    excluded_total = {r: 0 for r in EXCLUSION_ORDER}
    eligible_refs: List[str] = []
    total = eligible = 0

    for s in signals:
        total += 1
        fips = str(s.get("county_fips") or "unknown")
        pc = per_county.setdefault(fips, {"total": 0, "eligible": 0,
                                          "excluded": {r: 0 for r in EXCLUSION_ORDER}})
        pc["total"] += 1
        ok, code, _why = evaluate_mint_predicate(s, today)
        if ok:
            eligible += 1
            pc["eligible"] += 1
            eligible_refs.append(str(s.get("source_ref") or s.get("parcel_apn") or ""))
        else:
            excluded_total[code] += 1
            pc["excluded"][code] += 1

    body = {
        "as_of": today.isoformat(),
        "predicate_version": PREDICATE_VERSION,
        "total": total,
        "eligible": eligible,
        "excluded": excluded_total,
        "per_county": {f: per_county[f] for f in sorted(per_county)},
        "eligible_refs": sorted(eligible_refs),
        "reconciled": total == eligible + sum(excluded_total.values()),
    }
    body["receipt_sha256"] = section_hash(body)
    return body


def render_reconciliation(recon: Dict[str, Any]) -> str:
    """The human dry-run report: county-by-county eligible + excluded-by-reason."""
    L = [f"=== TAXSALE MINT RECONCILIATION — as of {recon['as_of']} "
         f"(predicate v{recon['predicate_version']}) ===",
         f"total={recon['total']}  eligible={recon['eligible']}  "
         + "  ".join(f"{r}={recon['excluded'][r]}" for r in EXCLUSION_ORDER)]
    for fips, pc in recon["per_county"].items():
        excl = "  ".join(f"{r}={pc['excluded'][r]}" for r in EXCLUSION_ORDER if pc["excluded"][r])
        L.append(f"  [{fips}] total={pc['total']} eligible={pc['eligible']}"
                 + (f"  excluded: {excl}" if excl else ""))
    L.append(f"reconciled={'YES' if recon['reconciled'] else 'NO — BOOKS DO NOT BALANCE'}  "
             f"receipt sha256:{recon['receipt_sha256'][:24]}…")
    return "\n".join(L)


def reconciliation_canonical(recon: Dict[str, Any]) -> str:
    """Canonical JSON of a reconciliation — what two dry-runs diff byte-for-byte."""
    return canonical_json(recon)
