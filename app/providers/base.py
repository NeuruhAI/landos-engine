"""
base.py — the ParcelProvider contract (DISCOVERY-R: vendor resilience).

The parcel-data layer sits behind this interface so NO single vendor cap can
halt discovery, enrichment, or cold-county ingest (lesson: ReportAll's
alltime 1,000-call quota exhausted mid-discovery, HTTP 429). ReportAll is ONE
provider behind the interface, not the spine.

Minimal surface (recon-proven: this is everything the engine's lookup paths
actually call):

    lookup_by_apn(county_fips, apn)        -> List[parcel dict]
    search_by_owner(county_fips, owner)    -> List[parcel dict]

Both return the HOUSE LEAD SHAPE — the exact dict schema produced by
app/normalizers/reportall_owner.ReportAllOwnerNormalizer.normalize()
(county_fips, parcel_apn = CANONICAL county APN [IN: 71-form, never 018],
reportall_parcel_id, situs_*, owner_*, land_use_class, acreage, provenance,
raw_meta). Providers REUSE that normalizer's logic — never redefine the shape.

Failure discipline (same as DISCOVERY-1's Yoder 429): a provider that cannot
answer raises ProviderUnavailable — it never guesses, never hard-stops the
chain. A truly-empty answer returns []. The chain (chain.py) degrades through
tiers and records which provider answered (audit-grade provenance).

A new vendor = a new module implementing this ABC + a config row
(app/providers/config.py) — never a framework rewrite (same doctrine as
app/normalizers and app/discovery).
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

ParcelDict = Dict[str, Any]


class ProviderUnavailable(Exception):
    """Provider cannot answer right now (quota, no key, no endpoint, network).
    The chain logs it and degrades to the next tier; retry later is valid."""


def name_tokens(person_name: str) -> Optional[str]:
    """'Yoder, Marilyn R.' -> 'YODER MARILYN' (tax-roll SURNAME FIRST form).
    Shared by the local-cache provider and the discovery pipeline."""
    d = (person_name or "").replace(".", " ").strip().upper()
    if "," in d:
        last, _, rest = d.partition(",")
        first = (rest.split() or [""])[0]
    else:
        parts = d.split()
        if len(parts) < 2:
            return None
        last, first = parts[-1], parts[0]
    last, first = last.strip(), first.strip()
    return f"{last} {first}".strip() if last and first else None


class ParcelProvider(abc.ABC):
    name: str          # 'local_cache' | 'county_rest' | 'reportall' | 'regrid'
    cost_note: str = ""  # human note for the chain's cost-gradient ordering

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    @abc.abstractmethod
    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        """apn = the CANONICAL county APN (IN: 71-form). Raise
        ProviderUnavailable when the provider cannot answer; [] when it can
        and there is genuinely no record."""

    @abc.abstractmethod
    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        """owner_name in source form ('Yoder, Marilyn R.' or 'YODER MARILYN').
        Same raise/empty discipline as lookup_by_apn."""
