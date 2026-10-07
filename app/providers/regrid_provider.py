"""
regrid_provider.py — tier 3: Regrid, the institutional-grade national parcel
source. DOCUMENTED STUB — dormant until a key exists (same pattern as the
Tracerfy backstop: the gate is real, the API contract is NOT guessed).

When armed (REGRID_API_KEY in .env):
  - Regrid's parcel API (app.regrid.com / api.regrid.com v2) serves national
    parcel records incl. owner, situs, and county APN — wire lookup_by_apn to
    the parcel-number endpoint and search_by_owner to the owner query, then
    map fields into the house lead shape (reuse the normalizer conventions:
    canonical county APN in parcel_apn).
  - Pricing is per-seat/per-call tiers — keep it LAST in the chain (config.py)
    so free tiers absorb the volume.

Until then every call raises ProviderUnavailable (chain degrades cleanly);
with a key present but no implementation, it fails LOUDLY instead of
fabricating a request.
"""
from __future__ import annotations

import os
from typing import List

from .base import ParcelProvider, ParcelDict, ProviderUnavailable


class RegridProvider(ParcelProvider):
    name = "regrid"
    cost_note = "premium paid national fallback — dormant until REGRID_API_KEY set"

    def _gate(self, target_key: str) -> None:
        key = (os.environ.get("REGRID_API_KEY") or "").strip()
        if not key:
            raise ProviderUnavailable(
                "regrid: dormant (no REGRID_API_KEY in .env) — documented stub; "
                "add the key AND implement the API mapping to arm")
        # Armed means paid: the FREE-FIRST guard stands in front of any future request.
        from app.free_first.guard import clear_paid_call
        clear_paid_call("regrid", items=[("parcel.owner_identity", target_key)], est_cost_usd=None,
                        caller="app/providers/regrid_provider.py:RegridProvider._gate")
        raise RuntimeError(
            "REGRID_API_KEY is set but the Regrid integration is not implemented "
            "yet — wire the v2 API mapping in regrid_provider.py before arming."
        )

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        self._gate(f"parcel:{county_fips}:{(apn or '').strip()}")
        return []   # unreachable

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        self._gate(f"owner:{county_fips}:{(owner_name or '').strip().upper()}")
        return []   # unreachable
