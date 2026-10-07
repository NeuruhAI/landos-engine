"""Repeatable $0 delta refresh. Re-enrich new or changed CURRENT identities only."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from .identity_tuple import identity_tuple_hash
from .t12_append import apn_equal


@dataclass
class DeltaPartition:
    estate_revision: str
    unchanged_ids: Set[str] = field(default_factory=set)
    changed_ids: Set[str] = field(default_factory=set)
    new_ids: Set[str] = field(default_factory=set)
    dropped_ids: Set[str] = field(default_factory=set)
    keep_rows: List[dict] = field(default_factory=list)
    rematch_rows: List[dict] = field(default_factory=list)

    @property
    def rematch_required(self) -> bool:
        return bool(self.changed_ids or self.new_ids)


def _ids(rows: Iterable[dict]) -> Set[str]:
    return {str(r.get("candidate_id") or "") for r in rows if r.get("candidate_id")}


def partition_delta(
    estate: List[dict],
    *,
    estate_revision: str,
    prior_append: Optional[List[dict]] = None,
    prior_hashes: Optional[Dict[str, str]] = None,
    prior_estate_ids: Optional[Set[str]] = None,
    prior_estate_revision: str = "",
) -> DeltaPartition:
    """Apply the sealed delta rule against the new estate E and prior HIGH append P."""
    current = {r["candidate_id"]: r for r in estate}
    current_ids = set(current)
    prior_append = prior_append or []
    prior_hashes = dict(prior_hashes or {})
    prior_by_cand: Dict[str, List[dict]] = defaultdict(list)
    for row in prior_append:
        cid = str(row.get("candidate_id") or "")
        if cid:
            prior_by_cand[cid].append(row)

    if prior_estate_ids is None:
        prior_estate_ids = set(prior_hashes) or set(prior_by_cand)
    dropped = set(prior_estate_ids) - current_ids
    new_ids = current_ids - set(prior_estate_ids)

    unchanged: Set[str] = set()
    changed: Set[str] = set()
    keep_rows: List[dict] = []

    same_revision = bool(prior_estate_revision) and prior_estate_revision == estate_revision

    for cid, row in current.items():
        current_hash = identity_tuple_hash(row)
        if cid in new_ids:
            changed.discard(cid)
            continue
        prior_hash = prior_hashes.get(cid, "")
        if prior_hash:
            if prior_hash == current_hash:
                unchanged.add(cid)
                keep_rows.extend(prior_by_cand.get(cid, []))
            else:
                changed.add(cid)
            continue
        if same_revision:
            unchanged.add(cid)
            keep_rows.extend(prior_by_cand.get(cid, []))
            continue
        # Fallback when the prior ledger has no hash: owner+apn+county must still agree.
        prior_rows = prior_by_cand.get(cid, [])
        if prior_rows and _append_identity_agrees(row, prior_rows[0]):
            unchanged.add(cid)
            keep_rows.extend(prior_rows)
        elif cid in prior_estate_ids and not prior_rows:
            # Previously classified, no HIGH phone. Without a hash, keep as unchanged
            # only when the prior estate id set is complete (not inferred from append).
            if prior_hashes or (prior_estate_ids - set(prior_by_cand)):
                unchanged.add(cid)
            else:
                changed.add(cid)
        else:
            changed.add(cid)

    rematch_ids = (new_ids | changed) & current_ids
    return DeltaPartition(
        estate_revision=estate_revision,
        unchanged_ids=unchanged,
        changed_ids=changed,
        new_ids=new_ids,
        dropped_ids=dropped,
        keep_rows=keep_rows,
        rematch_rows=[current[cid] for cid in rematch_ids],
    )


def _append_identity_agrees(estate_row: dict, append_row: dict) -> bool:
    if str(estate_row.get("candidate_id") or "") != str(append_row.get("candidate_id") or ""):
        return False
    if not apn_equal(str(estate_row.get("apn") or ""), str(append_row.get("apn") or "")):
        return False
    owner_a = str(estate_row.get("owner_name") or "").strip().upper()
    owner_b = str(append_row.get("owner_name") or "").strip().upper()
    if owner_a and owner_b and owner_a != owner_b:
        return False
    county_a = str(estate_row.get("county") or "").strip().upper()
    county_b = str(append_row.get("county") or "").strip().upper()
    if county_a and county_b and county_a != county_b:
        return False
    return True


def infer_prior_estate_ids(
    estate: List[dict],
    prior_append: List[dict],
    prior_missing: Optional[List[dict]] = None,
) -> Set[str]:
    """Rebuild the prior CURRENT id set from sealed T19F product files when no ledger exists."""
    ids = _ids(prior_append) | _ids(prior_missing or [])
    if ids:
        return ids
    return _ids(estate)


def refresh_receipt(
    partition: DeltaPartition,
    *,
    ncsbe_zip_sha256: List[str],
    high_appended: int,
    held_tier_c: int,
    local_safe_matches: int,
    classified_total: int,
    previous_high_reused: int = 0,
    new_high_appended: int = 0,
    medium_held: int = 0,
    review_held: int = 0,
    source_drift: Optional[List[str]] = None,
) -> dict:
    return {
        "estate_revision": partition.estate_revision,
        "ncsbe_zip_sha256": ncsbe_zip_sha256,
        "source_drift": sorted(source_drift or []),
        "candidates_total": (
            len(partition.unchanged_ids)
            + len(partition.changed_ids)
            + len(partition.new_ids)
        ),
        "candidates_new": len(partition.new_ids),
        "candidates_changed": len(partition.changed_ids),
        "candidates_unchanged": len(partition.unchanged_ids),
        "candidates_dropped": len(partition.dropped_ids),
        "candidates_rematch": len(partition.rematch_rows),
        "high_appended": high_appended,
        "previous_high_reused": previous_high_reused,
        "new_high_appended": new_high_appended,
        "medium_held": medium_held,
        "review_held": review_held,
        "held_tier_c": held_tier_c,
        "local_safe_matches": local_safe_matches,
        "classified_total": classified_total,
        "provider_spend": "$0.00",
        "production_mutated": "NO",
        "paid_provider_calls": 0,
        "seller_contact": 0,
        "dnc_runs": 0,
        "callability_authorized": "NO",
    }
