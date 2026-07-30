"""
load_signals.py — CC2 step 3: the SIGNAL LOADER (AXON Emergence Loop).

    python -m scripts.load_signals [--source taxsale|estate_sale|all] [--dry-run]

Lifts CC3's staged national signals (landos-ingest, read-only JSONL) into
landos.discovery_signals as TEMPORAL rows (status='new', unresolved). This is the bridge
that feeds the emergence pipe proven in step 2 with FRESH dated signals. Resolution (setting
matched_lead_id), scoring, and projection are separate steps.

Pure: reads local JSONL + writes landos.discovery_signals only. NO network, NO spend.

CC3 11-col contract -> landos.discovery_signals mapping:
    signal_id   (str, e.g. "stjoseph_taxsale:18141:...") -> source_ref  (natural key; the
                  uuid PK signal_id is left to DEFAULT gen_random_uuid())
    source_type ("taxsale" / "estate_sale")              -> source_type
    geography   ("18141" / "IL")                          -> county_fips
    event_date  (date or ISO datetime)                    -> signal_date (cast to date)
    parcel_apn  -> see canonical_apn(): for IN tax-sale the CANONICAL 71-form lives in
                  detail_json.parcel_state_number; CC3's top-level parcel_apn is the 018-form
                  ReportAll-internal id, which NEVER matches owner_leads (DISCOVERY_ADAPTERS.md
                  invariant 1). We store the 71-form; the 018-form is preserved in raw_payload.
    owner_name  -> owner_name (empty -> NULL)
    detail_json -> raw_payload (full jsonb; keeps both APN forms + fetched_at provenance)
    confidence  -> confidence
    source / detail_json url -> source_url (provenance)
  Constants: signal_class='temporal' (asserted), status='new', tenant_id=NULL (national
  signals are tenant-agnostic until resolution links one to a tenant's owner_lead),
  matched_lead_id left NULL (the resolver's job), ingested_at DEFAULT now().

Idempotent on the natural key: ON CONFLICT (source_type, source_ref) DO NOTHING — re-running
inserts 0 duplicates and never clobbers a row that has since been resolved (matched_lead_id /
status are preserved by DO NOTHING). NB: the directive said "ON CONFLICT (signal_id)", but
CC3's signal_id is a string and the table's real uniqueness is uq_discovery_signal_natural
on (source_type, source_ref) — that is the correct conflict target.
"""
import argparse
import json
import os
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.freshness import upcoming_taxsale

SIGNAL_CLASS = "temporal"
# Canonical landos-ingest repo. (~/repos/landos-ingest is only a personal symlink to
# this path — depending on it silently breaks on any host without that symlink.)
INGEST_ROOT = os.path.expanduser("~/neuruh-repos/NeuruhAI/landos-ingest/data/strategy/signals")
SOURCES = {
    "taxsale":        os.path.join(INGEST_ROOT, "tax_sale/staged/stjoseph_taxsale_signals.jsonl"),
    "estate_sale":    os.path.join(INGEST_ROOT, "estate_sales/staged/estatesales_signals.jsonl"),
    # LANE D (t5 commit b504dfc): two new source_types. code_violation is ARMED
    # (stjoseph, 237 signals, 237/237 canonical parcel match); lis_pendens is
    # DISARMED (ToU-gated operator_file drop — staged file absent until a legally
    # obtained export lands). A missing file is skipped, not fatal (see main()).
    "code_violation": os.path.join(INGEST_ROOT, "code_violation/staged/stjoseph_code_violation_signals.jsonl"),
    "lis_pendens":    os.path.join(INGEST_ROOT, "lis_pendens/staged/stjoseph_lis_pendens_signals.jsonl"),
    # ENGINE CROSS-FEED §5 (landos-ingest docs/engine_cross_feeds.md): rental_license is
    # PARCEL-DIRECT / directly-usable — the generic map_row stores it as-is (71-form
    # parcel_apn from detail_json.parcel_state_number; landlord_exit / absentee_rental /
    # data_vintage ride into raw_payload). 1,347 staged (509 landlord-exit, all absentee).
    # HISTORIC/2024 vintage: many rows carry no event_date -> signal_date NULL (honest, not
    # backfilled). Value = the landlord_exit subset crossed with absentee + tax-delinquent
    # for motivated-landlord ranking (t2 cross; no resolver needed to LOAD).
    "rental_license": os.path.join(INGEST_ROOT, "rental_license/staged/stjoseph_rental_license_signals.jsonl"),
    # FLEET LOAD 2026-07-13 (cross-feed §§1-3). A lane may stage MULTIPLE county files —
    # values here may be a list; main() normalizes. All three ride the generic map_row
    # (11-col contract). rental_license precedent governs the class: facts load as
    # temporal with honest-NULL fields, resolution is a separate step.
    #   ground_lease  (§1, spatial_deferred): parcel_apn empty by design; detail.lat/lng
    #     PIP-resolves to the HOST parcel engine-side. ⚠ owner_name = FCC registrant
    #     (tower company), NEVER the lead — the host parcel's assessor owner is.
    #   flood_remap   (§2, community_macro): community-grain (FEMA CID), parcel_apn empty;
    #     resolution = community join, NOT parcel PIP.
    #   layoff_notice (§3, area_deferred): geography='18' = STATE-grain (honest, per
    #     contract); county_fips lands as '18'. LOAD ONLY — the WARN ZIP-heat resolver and
    #     any WARN scoring are UNGREEN (operator gate); nothing downstream may consume
    #     unresolved WARN rows (matched_lead_id stays NULL).
    "ground_lease": [
        os.path.join(INGEST_ROOT, "ground_lease/staged/stjoseph_ground_lease_signals.jsonl"),
        os.path.join(INGEST_ROOT, "ground_lease/staged/berrien_ground_lease_signals.jsonl"),
    ],
    "flood_remap": [
        os.path.join(INGEST_ROOT, "flood_remap/staged/stjoseph_flood_remap_signals.jsonl"),
        os.path.join(INGEST_ROOT, "flood_remap/staged/berrien_flood_remap_signals.jsonl"),
    ],
    "layoff_notice": os.path.join(INGEST_ROOT, "layoff_notice/staged/indiana_warn_signals.jsonl"),
}

INSERT = """
    insert into landos.discovery_signals
        (tenant_id, source_type, source_ref, source_url, county_fips,
         raw_payload, parcel_apn, owner_name, signal_date, confidence,
         status, signal_class)
    values (%(tenant_id)s, %(source_type)s, %(source_ref)s, %(source_url)s, %(county_fips)s,
            %(raw_payload)s, %(parcel_apn)s, %(owner_name)s, %(signal_date)s, %(confidence)s,
            %(status)s, %(signal_class)s)
    on conflict (source_type, source_ref) do nothing
"""


def to_date(s):
    """'2026-03-13' or '2026-06-22T14:00:00.000Z' -> date (time truncated)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(str(s)[:10])


def canonical_apn(rec):
    """Canonical county tax APN for the parcel_apn column. For IN tax-sale that is the
    71-form in detail_json.parcel_state_number; CC3's top-level parcel_apn is the 018-form
    ReportAll-internal id, which NEVER matches owner_leads (DISCOVERY_ADAPTERS.md inv.1).
    Synthetic ids (estate 'estate-<listing>') are NOT real parcels -> NULL."""
    dj = rec.get("detail_json") or {}
    apn = (dj.get("parcel_state_number") or rec.get("parcel_apn") or "").strip()
    return apn or None


def source_url(rec):
    dj = rec.get("detail_json") or {}
    return dj.get("url") or dj.get("tax_info_url") or dj.get("source_layer")


def map_row(rec):
    return {
        "tenant_id": None,                                  # national — tenant acquired at resolution
        "source_type": rec["source_type"],
        "source_ref": rec["signal_id"],                     # CC3 signal_id (str) = natural key
        "source_url": source_url(rec),
        "county_fips": rec.get("geography"),
        "raw_payload": Jsonb(rec.get("detail_json") or {}),
        "parcel_apn": canonical_apn(rec),
        "owner_name": (rec.get("owner_name") or "").strip() or None,
        "signal_date": to_date(rec.get("event_date")),
        "confidence": rec.get("confidence"),
        "status": "new",
        "signal_class": SIGNAL_CLASS,
    }


def load_file(cur, path, dry_run):
    read = inserted = skipped = rejected = 0
    today = date.today()
    with open(path) as fh:                                  # READ-ONLY on CC3's tree
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # FRESHNESS GATE (gate-in-data): only UPCOMING, unresolved tax-sale parcels pass.
            # A consumed/past or already-reverted sale is a spent signal — reject, never load.
            if rec.get("source_type") == "taxsale":
                ok, why = upcoming_taxsale(rec.get("event_date"), rec.get("detail_json"),
                                           rec.get("owner_name"), today)
                if not ok:
                    rejected += 1
                    print(f"  REJECT taxsale {rec.get('signal_id')!r}: {why}")
                    continue
            row = map_row(rec)
            # RAIL (assert): this loader is TEMPORAL-only. A structural row must never enter.
            assert row["signal_class"] == "temporal", f"non-temporal row {rec.get('signal_id')!r}"
            assert not str(row["source_type"]).startswith("graph"), \
                f"structural source_type {row['source_type']!r} reached the temporal loader"
            read += 1
            if dry_run:
                # would this row be new? (existence check, no write)
                cur.execute("select 1 from landos.discovery_signals "
                            "where source_type=%s and source_ref=%s",
                            (row["source_type"], row["source_ref"]))
                if cur.fetchone():
                    skipped += 1
                else:
                    inserted += 1
                continue
            cur.execute(INSERT, row)
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
    return read, inserted, skipped, rejected


def main(sources, dry_run):
    tot_read = tot_ins = tot_skip = tot_rej = 0
    loaded_files = missing_files = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for key in sources:
            spec = SOURCES[key]
            for path in (spec if isinstance(spec, list) else [spec]):
                if not os.path.exists(path):
                    # A disarmed/not-yet-staged source (or reorganized upstream tree) is
                    # an honest skip, never a crash — the loader must survive `--source all`.
                    print(f"  [{key}] SKIP — no staged file at {path}")
                    missing_files += 1
                    continue
                r, i, s, rej = load_file(cur, path, dry_run)
                loaded_files += 1
                verb = "would_insert" if dry_run else "inserted"
                print(f"  [{key}] {os.path.basename(path)}: read={r} {verb}={i} "
                      f"skipped_dup={s} rejected_stale={rej}")
                tot_read += r; tot_ins += i; tot_skip += s; tot_rej += rej
        if dry_run:
            conn.rollback()
            print(f"\n=== LOAD DRY-RUN — read {tot_read}, would_insert {tot_ins}, "
                  f"skipped_dup {tot_skip}, rejected_stale {tot_rej}. NO writes. ===")
            return {
                "read": tot_read, "would_insert": tot_ins,
                "inserted": 0, "skipped_dup": tot_skip,
                "rejected_stale": tot_rej, "loaded_files": loaded_files,
                "missing_files": missing_files,
            }
        conn.commit()
    print(f"\nload summary: read={tot_read} inserted={tot_ins} skipped_dup={tot_skip} "
          f"rejected_stale={tot_rej} (taxsale freshness gate: only upcoming/unresolved pass; "
          f"signal_class=temporal, status=new, tenant_id=NULL).")
    return {
        "read": tot_read, "would_insert": 0, "inserted": tot_ins,
        "skipped_dup": tot_skip, "rejected_stale": tot_rej,
        "loaded_files": loaded_files, "missing_files": missing_files,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="CC2 step 3 — lift CC3 staged signals into landos.discovery_signals (temporal)")
    ap.add_argument("--source", choices=list(SOURCES) + ["all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    a = ap.parse_args()
    srcs = list(SOURCES) if a.source == "all" else [a.source]
    main(srcs, a.dry_run)
