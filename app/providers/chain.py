"""
chain.py — the ordered provider fallback resolver (DISCOVERY-R).

Tries each tier in the config-driven order (cost gradient, cheapest first),
returns on the FIRST non-empty answer, and stamps every returned parcel with
`which_provider` (+ a chain-prefixed provenance) so the system always knows
where a match came from — audit-grade.

Degrade discipline:
  - ProviderUnavailable (quota/429, no key, no endpoint, unverified config,
    network) -> log the tier, move to the next. NEVER a hard stop.
  - empty answer from a live tier -> move on (local-cache miss must fall
    through to the network tiers).
  - full-chain miss -> [] — and `last_trail` tells the caller WHY: if any
    tier was 'unavailable' the miss is RETRYABLE (signal stays 'new'); if
    every live tier answered 'empty' it is a true no_match. Never a guess.

Headless/API-first: usable identically by the discovery pipeline, the
enrichment path, or any future service endpoint.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .base import ParcelProvider, ParcelDict
from .base import ProviderUnavailable
from .config import PARCEL_PROVIDER_CHAIN
from .local_cache import LocalCacheProvider
from .county_rest import CountyRESTProvider
from .fetchgis import FetchGISProvider
from .colligo import ColligoProvider
from .reportall_provider import ReportAllProvider
from .regrid_provider import RegridProvider

PROVIDER_REGISTRY = {
    "local_cache": LocalCacheProvider,
    "county_rest": CountyRESTProvider,
    "fetchgis": FetchGISProvider,     # tier 1b: FetchGIS PSsearch2 (ENRICH-1)
    "colligo": ColligoProvider,       # tier 1c: Colligo GIS public links (ENRICH-1)
    "reportall": ReportAllProvider,
    "regrid": RegridProvider,
}


class ParcelProviderChain:
    def __init__(self, tenant_id: str, conn=None):
        self.tenant_id = tenant_id
        self._instances = {}
        self._conn = conn               # shared into providers that take one
        # (provider_name, status, detail) per tier of the LAST call
        self.last_trail: List[Tuple[str, str, str]] = []

    def _provider(self, name: str) -> ParcelProvider:
        if name not in self._instances:
            cls = PROVIDER_REGISTRY[name]
            self._instances[name] = (cls(self.tenant_id, conn=self._conn)
                                     if name == "local_cache" else cls(self.tenant_id))
        return self._instances[name]

    def order_for(self, county_fips: str) -> List[str]:
        return PARCEL_PROVIDER_CHAIN.get(county_fips,
                                         PARCEL_PROVIDER_CHAIN["default"])

    def _run(self, county_fips: str, op: str, *args) -> List[ParcelDict]:
        self.last_trail = []
        for name in self.order_for(county_fips):
            provider = self._provider(name)
            try:
                results = getattr(provider, op)(county_fips, *args)
            except ProviderUnavailable as e:
                self.last_trail.append((name, "unavailable", str(e)[:140]))
                continue
            if results:
                self.last_trail.append((name, "hit", f"{len(results)} parcel(s)"))
                for r in results:
                    r["which_provider"] = name
                    r["provenance"] = f"chain:{name}|{r.get('provenance', '')}"
                return results
            self.last_trail.append((name, "empty", "no records"))
        return []

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        return self._run(county_fips, "lookup_by_apn", apn)

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        return self._run(county_fips, "search_by_owner", owner_name)

    # ---- miss semantics --------------------------------------------------------
    def miss_is_retryable(self) -> bool:
        """True when the last full-chain miss involved an unavailable tier —
        the answer may exist behind a quota/key; the caller should keep the
        signal retryable instead of declaring no_match."""
        return any(s == "unavailable" for _, s, _ in self.last_trail)

    def trail_str(self) -> str:
        return " -> ".join(f"{n}:{s}" for n, s, _ in self.last_trail)
