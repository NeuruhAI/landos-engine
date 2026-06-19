"""
reportall_provider.py — tier 2: ReportAll as ONE provider behind the interface.

ZERO behavior change: wraps the existing ReportAllOwnerNormalizer client —
same _search, same normalize() (which owns the 71-form/018-form split for
reportall_native counties), same fatal-error semantics. The only new logic is
translation of quota/HTTP failures into ProviderUnavailable so the chain
degrades instead of hard-stopping (the alltime-quota 429 of 2026-06-10 is the
motivating failure).
"""
from __future__ import annotations

from typing import List

from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
from .base import ParcelProvider, ParcelDict, ProviderUnavailable, name_tokens


class ReportAllProvider(ParcelProvider):
    name = "reportall"
    cost_note = "metered vendor API (alltime quota per key)"

    def __init__(self, tenant_id: str):
        super().__init__(tenant_id)
        self._clients = {}          # county_fips -> ReportAllOwnerNormalizer
        self.calls_made = 0

    def _client(self, county_fips: str) -> ReportAllOwnerNormalizer:
        if county_fips not in self._clients:
            try:
                self._clients[county_fips] = ReportAllOwnerNormalizer(
                    self.tenant_id, county_fips)
            except ValueError as e:    # county not in COUNTY_CONFIG
                raise ProviderUnavailable(str(e)) from e
        return self._clients[county_fips]

    def _guarded(self, ra, extra, label) -> dict:
        try:
            data = ra._search(extra, label)
        except RuntimeError as e:      # HTTP 429 / quota / network / API error
            raise ProviderUnavailable(f"reportall: {e}") from e
        self.calls_made = ra.calls_made
        return data

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        ra = self._client(county_fips)
        data = self._guarded(ra, {"parcel_id": apn, "rpp": 5, "page": 1}, f"apn={apn}")
        hits = [(r.get("parcel_id"), r) for r in (data.get("results") or [])]
        return ra.normalize(hits)

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        ra = self._client(county_fips)
        q = name_tokens(owner_name) or (owner_name or "").strip().upper()
        if not q:
            return []
        data = self._guarded(ra, {"owner": q, "rpp": 25, "page": 1}, f"owner~{q}")
        toks = q.split()
        hits = []
        for r in data.get("results") or []:
            owner = (r.get("owner") or "").upper()
            if all(t in owner.split() for t in toks):
                hits.append((r.get("parcel_id"), r))
        return ra.normalize(hits)
