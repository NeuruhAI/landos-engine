"""
mint_recipe_leads.py — recipe -> owner_leads intake (senior_lakefront_assemblage step 9).

    python -m scripts.mint_recipe_leads --csv <cluster_parcels.csv> [--run-id ID] [--dry-run]

Transforms a recipe's cluster_parcels.csv (spec §9.2) into the EXISTING intake shape:
landos.owner_leads rows with provenance='discovered_recipe' and source='recipe:<recipe_id>'.
sync_to_crm.py — THE single writer of public.acquisition_leads — then projects them on its
next operator-fired run through the same discovered-tier path as the tax-sale mint. This
script adds NO second writer and NEVER touches public.acquisition_leads.

provenance='discovered_recipe' (not 'recipe:...') is deliberate: the sync's intake gate is
provenance.startswith('discovered'); a bare 'recipe:' provenance would warn-and-skip
forever. 'recipe:<recipe_id>' rides the source column instead (§9.2), and the prefix is
distinct from 'discovered_taxsale' (mig-015 scoping) and 'reportall:%' (mig-004 seed tag).

Idempotent: ON CONFLICT (tenant_id, county_fips, parcel_apn) DO NOTHING — re-run mints 0
dups; a parcel already in the book keeps its existing lead untouched (additive-only).
Batch removal if ever needed is the operator's:
DELETE where provenance='discovered_recipe' and source='recipe:<recipe_id>'.

GATED: live mode fires only on the operator's keystroke AFTER the §10 spot-check
worklist passes. --dry-run proves the full §9.2 mapping (owner_leads params + the
projection sync_to_crm will compose) with zero writes.
"""
import argparse
import csv
import json
import pathlib

from app.score_absentee import classify_owner_type
from scripts.sync_to_crm import (
    is_recipe_meta, parcel_meta_from_recipe, recipe_signal_tags,
    recipe_source, score_signals_from_recipe,
)

APG_TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
PROVENANCE = "discovered_recipe"

MINT = """
    insert into landos.owner_leads
        (tenant_id, county_fips, parcel_apn, owner_name, situs_address,
         situs_lat, situs_lng, owner_mailing_address, owner_mailing_city,
         owner_mailing_state, owner_mailing_zip, owner_type,
         discovery_seed, source, provenance, raw_meta)
    values (%(tenant_id)s, %(county_fips)s, %(parcel_apn)s, %(owner_name)s, %(situs_address)s,
            %(situs_lat)s, %(situs_lng)s, %(owner_mailing_address)s, %(owner_mailing_city)s,
            %(owner_mailing_state)s, %(owner_mailing_zip)s, %(owner_type)s,
            false, %(source)s, %(provenance)s, %(raw_meta)s)
    on conflict (tenant_id, county_fips, parcel_apn) do nothing
    returning lead_id
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _bool(v):
    return str(v or "").strip().lower() in ("true", "1", "t")


def _text(v):
    """'' -> None: a blank CSV cell must land as honest NULL, never empty string."""
    s = (v or "").strip()
    return s or None


def map_csv_row(row: dict, run_id: str) -> dict:
    """One cluster_parcels.csv row -> owner_leads intake params (§9.2). Pure; unit-tested.

    raw_meta stows the whole recipe payload; recipe_id doubles as the marker
    sync_to_crm.is_recipe_meta keys on. tenure_years '' = SDF-window-dark ->
    None (floor-scored upstream), never a fabricated 0. absentee/acreage/
    confidence/final_lead_score = honest NULL (no source for them here).
    """
    recipe_id = (row.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError(f"row {row.get('parcel_apn')!r} has no recipe_id — not a recipe CSV")
    owner_name = _text(row.get("owner_name"))
    owner_kind = _text(row.get("owner_kind"))
    raw_meta = {
        "recipe_id": recipe_id,
        "run_id": run_id,
        "senior_score": _num(row.get("senior_score")),
        "tenure_years": _num(row.get("tenure_years")),
        "dist_to_lake_m": _num(row.get("dist_to_lake_m")),
        "cluster_id": _int(row.get("cluster_id")),
        "cluster_size": _int(row.get("cluster_size")),
        "same_surname_family": _bool(row.get("same_surname_family")),
        "signal_tags": [t for t in (row.get("signal_tags") or "").split("|") if t],
        "owner_kind": owner_kind,
        "owner_occupied_proxy": _int(row.get("owner_occupied_proxy")),
        "tenure_floor": _int(row.get("tenure_floor")),
        "surname": _text(row.get("surname")),
    }
    return {
        "tenant_id": APG_TENANT,
        "county_fips": (row.get("county_fips") or "").strip(),
        "parcel_apn": (row.get("parcel_apn") or "").strip(),
        "owner_name": owner_name,
        "situs_address": _text(row.get("situs_address")),
        "situs_lat": _num(row.get("situs_lat")),
        "situs_lng": _num(row.get("situs_lng")),
        "owner_mailing_address": _text(row.get("owner_mailing_address")),
        "owner_mailing_city": _text(row.get("owner_mailing_city")),
        "owner_mailing_state": _text(row.get("owner_mailing_state")),
        "owner_mailing_zip": _text(row.get("owner_mailing_zip")),
        # the recipe's entity sweep already ran (3 iterations); 'human' is authoritative,
        # anything else falls back to the shared classifier
        "owner_type": "individual" if owner_kind == "human" else classify_owner_type(owner_name),
        "source": f"recipe:{recipe_id}",
        "provenance": PROVENANCE,
        "raw_meta": raw_meta,          # dict here; Jsonb-wrapped at execute time
    }


def main(csv_path: str, run_id: str = None, dry_run: bool = False):
    path = pathlib.Path(csv_path)
    run_id = run_id or path.parent.name
    with open(path, newline="", encoding="utf-8") as f:
        rows = [map_csv_row(r, run_id) for r in csv.DictReader(f)]

    if dry_run:
        print(f"=== RECIPE MINT DRY-RUN — {len(rows)} rows mapped from {path.name} "
              f"(run_id={run_id}), NO writes ===")
        for m in rows[:5]:
            rm = m["raw_meta"]
            assert is_recipe_meta(rm), "raw_meta must satisfy the sync's recipe marker"
            print(f"\n[{m['county_fips']}] {m['parcel_apn']}  {m['owner_name']!r} [{m['owner_type']}]")
            print(f"  owner_leads: provenance={m['provenance']!r} source={m['source']!r} "
                  f"situs={m['situs_address']!r} mailing={m['owner_mailing_city']},{m['owner_mailing_state']}")
            print(f"  -> acquisition_leads.source        = {recipe_source(rm)!r}")
            print(f"  -> acquisition_leads.signal_tags   = {recipe_signal_tags(rm)}")
            print(f"  -> acquisition_leads.score_signals = {json.dumps(score_signals_from_recipe(rm))}")
            print(f"  -> acquisition_leads.parcel_meta   = {json.dumps(parcel_meta_from_recipe(rm))}")
        print(f"\nHARD STOP — dry-run only (nothing written, no DB touched). "
              f"Live mode is GATED on the §10 spot-check PASS (operator keystroke).")
        return {"dry_run": True, "mapped": len(rows)}

    import psycopg                                    # deferred: dry-run needs no DB at all
    from psycopg.types.json import Jsonb
    from app.config import DATABASE_URL
    minted = already_present = 0
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        for m in rows:
            cur.execute(MINT, {**m, "raw_meta": Jsonb(m["raw_meta"])})
            if cur.fetchone():
                minted += 1
            else:
                already_present += 1
        conn.commit()
    print(f"\nrecipe mint summary: minted={minted} already_present={already_present} "
          f"(tenant=APG, provenance={PROVENANCE!r}, source per-row 'recipe:<recipe_id>', "
          f"run_id={run_id}; situs/mailing carried from the recipe CSV; absentee/"
          f"final_lead_score/confidence = honest NULL).")
    print("Next: `python -m scripts.sync_to_crm` (the single writer) projects them "
          "to public.acquisition_leads as status='discovered'.")
    return {"dry_run": False, "minted": minted, "already_present": already_present}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="recipe cluster_parcels.csv -> owner_leads intake (no CRM writes)")
    ap.add_argument("--csv", required=True, help="path to the recipe run's cluster_parcels.csv")
    ap.add_argument("--run-id", help="recipe run id (default: csv parent directory name)")
    ap.add_argument("--dry-run", action="store_true", help="prove the §9.2 mapping on 5 rows; write nothing")
    a = ap.parse_args()
    main(a.csv, run_id=a.run_id, dry_run=a.dry_run)
