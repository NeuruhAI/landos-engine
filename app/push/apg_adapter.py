"""
apg_adapter.py — push owner LEADS to APG's Supabase. BUILT, GATED, NOT FIRED.

This adapter is complete: connection wiring, field mapping, destination validation,
and the write path are all here and ready. But it is GATED at two independent
points so it cannot write in this build:

  1. do_push defaults to False everywhere (base.PushAdapter.push). Off => it only
     prints disabled_message and returns. This is the primary gate.
  2. Even with do_push=True, validate_destination() RAISES, because the owner-lead
     destination table on APG is NOT YET CONFIRMED with Bryan. See below.

CRITICAL — WRONG TABLE GUARD:
  APG's `builder_buyers` table is for builder BUYERS (demand side). Owner LEADS are
  the SUPPLY side. They must NOT go to builder_buyers. The correct destination table
  does not exist yet / its shape is unconfirmed, so DEST_TABLE is intentionally None
  and `builder_buyers` is hard-blocked.

CONNECTION:
  Reads APG_DATABASE_URL from the environment. It is intentionally NOT set in this
  project's .env, so _connect() cannot reach APG's DB in this build. Nothing here
  connects to or mutates APG's database now.

TO FIRE LATER (what unblocks the push):
  - Bryan confirms the owner-lead destination table name + column shape on APG
    Supabase (project nsxfcgxuglixxzgkwunc).
  - Set DEST_TABLE to that table (and adjust map_lead's column names to match).
  - Provide APG_DATABASE_URL in the environment.
  - Run the push CLI with --push.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import PushAdapter

# APG Supabase project ref (for reference/audit only — we do NOT connect in this build).
APG_PROJECT_REF = "nsxfcgxuglixxzgkwunc"

# Confirmed owner-lead destination table on APG. None == UNCONFIRMED (see module doc).
DEST_TABLE: Optional[str] = None

# Hard block — owner LEADS (supply) must never be written to a BUYERS table.
FORBIDDEN_TABLES = {"builder_buyers"}


class ApgPushAdapter(PushAdapter):
    target_id = "apg"

    def __init__(self, dest_table: Optional[str] = None):
        # allow an explicit override ONLY once Bryan confirms; defaults to UNCONFIRMED
        self.dest_table = dest_table if dest_table is not None else DEST_TABLE

    def map_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Map a landos owner_lead -> APG owner-lead row shape. Field NAMES on the
        right are provisional until Bryan confirms the table; the mapping itself is
        the stable, reviewable contract (supply-side fields only — no buyer fields)."""
        return {
            "source_engine": "landos:owner_outreach",
            "external_lead_id": str(lead.get("lead_id")),
            "county_fips": lead.get("county_fips"),
            "parcel_apn": lead.get("parcel_apn"),
            "owner_name": lead.get("owner_name"),
            "owner_type": lead.get("owner_type"),
            "mailing_address": lead.get("owner_mailing_address"),
            "mailing_city": lead.get("owner_mailing_city"),
            "mailing_state": lead.get("owner_mailing_state"),
            "mailing_zip": lead.get("owner_mailing_zip"),
            "situs_address": lead.get("situs_address"),
            "land_use_class": lead.get("land_use_class"),
            "acreage": lead.get("acreage"),
            "absentee_tier": lead.get("absentee_tier"),
            "absentee_score": lead.get("absentee_score"),
            "lead_score": lead.get("final_lead_score"),
            "signal_overlap": lead.get("signal_overlap"),
        }

    def validate_destination(self) -> None:
        if self.dest_table is None:
            raise RuntimeError(
                "APG owner-lead destination table is UNCONFIRMED. Refusing to push. "
                "Confirm the table name + shape with Bryan, then set DEST_TABLE "
                "(NOT builder_buyers — that is the buyers/demand table)."
            )
        if self.dest_table in FORBIDDEN_TABLES:
            raise RuntimeError(
                f"Refusing to push owner LEADS to {self.dest_table!r}: that is APG's "
                f"BUYERS table (demand). Owner leads are supply — wrong destination."
            )

    def _connect(self):
        url = os.environ.get("APG_DATABASE_URL")
        if not url:
            raise RuntimeError(
                "APG_DATABASE_URL is not set. This build does not connect to APG's "
                "database. Provide the connection string only when ready to fire."
            )
        import psycopg  # local import: never needed on the gated default path
        return psycopg.connect(url)

    def _write_rows(self, conn, rows: List[Dict[str, Any]]) -> int:
        # Reached only after both gates pass AND a real connection exists.
        if not rows:
            return 0
        cols = list(rows[0].keys())
        collist = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        n = 0
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    f"insert into public.{self.dest_table} ({collist}) values ({placeholders}) "
                    f"on conflict (external_lead_id) do update set lead_score=excluded.lead_score",
                    [row[c] for c in cols],
                )
                n += 1
        conn.commit()
        return n

    def disabled_message(self, n: int) -> str:
        return (
            f"APG PUSH DISABLED — destination table unconfirmed, would push {n} leads, "
            f"awaiting Bryan's table shape (APG project {APG_PROJECT_REF}; "
            f"NOT builder_buyers)."
        )
