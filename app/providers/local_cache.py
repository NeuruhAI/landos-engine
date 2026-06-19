"""
local_cache.py — tier 0: the quota-free landos.owner_leads lookup.

This is DISCOVERY-1's proven local-first match (it answered the Steward case
with zero vendor calls) promoted to a ParcelProvider. It can only return
parcels the engine already holds — a miss here means "not in our universe
yet", so the chain falls through to network tiers.

Hits reference EXISTING leads: each dict carries `_existing_lead_id` so the
discovery pipeline FLAGS the existing row instead of creating a duplicate.
Reads landos only; writes nothing.
"""
from __future__ import annotations

from typing import List

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from .base import ParcelProvider, ParcelDict, ProviderUnavailable, name_tokens


class LocalCacheProvider(ParcelProvider):
    name = "local_cache"
    cost_note = "free — parcels already in landos.owner_leads"

    def __init__(self, tenant_id: str, conn=None):
        super().__init__(tenant_id)
        self._conn = conn          # reuse the pipeline's connection when given
        self._own_conn = False

    def _cur(self):
        if self._conn is None:
            self._conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            self._own_conn = True
        return self._conn.cursor(row_factory=dict_row)

    @staticmethod
    def _to_parcel(row) -> ParcelDict:
        return {
            "county_fips": row["county_fips"],
            "parcel_apn": row["parcel_apn"],
            "owner_name": row["owner_name"],
            "situs_address": row["situs_address"],
            "provenance": "local:owner_leads",
            "_existing_lead_id": row["lead_id"],   # flag, don't re-create
        }

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        try:
            with self._cur() as cur:
                cur.execute(
                    "select lead_id, county_fips, parcel_apn, owner_name, situs_address "
                    "from landos.owner_leads "
                    "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                    (self.tenant_id, county_fips, apn),
                )
                return [self._to_parcel(r) for r in cur.fetchall()]
        except psycopg.Error as e:
            raise ProviderUnavailable(f"local cache DB error: {e}") from e

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        q = name_tokens(owner_name) or (owner_name or "").strip().upper()
        toks = [t for t in q.split() if t]
        if not toks:
            return []
        try:
            with self._cur() as cur:
                clauses = " and ".join(["owner_name ilike %s"] * len(toks))
                cur.execute(
                    f"select lead_id, county_fips, parcel_apn, owner_name, situs_address "
                    f"from landos.owner_leads "
                    f"where tenant_id=%s and county_fips=%s and {clauses}",
                    (self.tenant_id, county_fips, *[f"%{t}%" for t in toks]),
                )
                return [self._to_parcel(r) for r in cur.fetchall()]
        except psycopg.Error as e:
            raise ProviderUnavailable(f"local cache DB error: {e}") from e
