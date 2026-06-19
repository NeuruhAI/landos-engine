"""
base.py — the DiscoveryAdapter contract (DISCOVERY-1).

The discovery layer turns motivated-seller SIGNALS into candidate leads.
Every source (probate, obituary, estate sale, permit, tax-delinquent,
code-violation) is ONE adapter module implementing DiscoveryAdapter —
a new source is a new adapter, never a framework rewrite (same doctrine as
app/normalizers and the per-state pierce adapters).

The adapter's ONLY job is signals:

    fetch() -> list of normalized signal dicts:
        {source_ref, source_url, owner_name, signal_date, confidence,
         raw_payload, parcel_apn (optional — only if the source itself
         carries the canonical county APN)}

Parcel matching and lead creation belong to the PIPELINE
(scripts/ingest_discovery.py), not the adapter — so match logic improves in
one place for every source. See DISCOVERY_ADAPTERS.md for the full contract,
access modes, and per-source gotchas (incl. the IN 71-form vs 018-form APN
trap and the prefix-pagination silent-miss limitation).
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List

from psycopg.types.json import Jsonb

DiscoverySignal = Dict[str, Any]


class DiscoveryAdapter(abc.ABC):
    # ---- identity (subclasses set these) -------------------------------------
    source_type: str            # e.g. 'probate_in' — also the provenance prefix
    county_fips: str            # the county this adapter instance serves
    access_mode: str            # 'automated' | 'va_fed' | 'bulk_dua'
    access_path: str            # the exact public access point (URL / portal)
    recipe_hint: str = ""       # outreach recipe the signal implies (probate -> R4)

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    @abc.abstractmethod
    def fetch(self) -> List[DiscoverySignal]:
        """Return normalized signal rows. PUBLIC records only, ToS-clean only.
        A va_fed adapter reads the VA/operator-supplied file instead of the
        network — same output shape, so the pipeline cannot tell the difference."""

    # ---- shared, idempotent persistence --------------------------------------
    def upsert_signals(self, cur, signals: List[DiscoverySignal]) -> int:
        """Insert/update landos.discovery_signals keyed on the source's natural
        ref. Re-running an ingest updates rows in place — never duplicates."""
        n = 0
        for s in signals:
            cur.execute(
                """
                insert into landos.discovery_signals
                  (tenant_id, source_type, source_ref, source_url, county_fips,
                   raw_payload, parcel_apn, owner_name, signal_date, confidence)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (source_type, source_ref) do update set
                   source_url = excluded.source_url,
                   raw_payload = excluded.raw_payload,
                   parcel_apn = coalesce(excluded.parcel_apn,
                                         landos.discovery_signals.parcel_apn),
                   owner_name = excluded.owner_name,
                   signal_date = excluded.signal_date,
                   confidence = excluded.confidence
                returning signal_id
                """,
                (self.tenant_id, self.source_type, s["source_ref"],
                 s.get("source_url"), self.county_fips, Jsonb(s.get("raw_payload") or {}),
                 s.get("parcel_apn"), s.get("owner_name"), s.get("signal_date"),
                 s.get("confidence", 0.5)),
            )
            n += 1
        return n
