"""
mint_discovery.py — the generalized, lens-aware DISCOVERY mint (W0 JOB 3).

    python -m scripts.mint_discovery --source mine_absentee --dry-run   # count only
    python -m scripts.mint_discovery --all --dry-run                     # every discovery source
    python -m scripts.mint_discovery --source mine_entity                # GATED fire (writes owner_leads)

One writer, many sources. Every APN-resolved DISCOVERY signal/mine -> a candidate
in landos.owner_leads with provenance='discovered_signal',
source='signal:<source_type>', and its lens carried in raw_meta.signal_tags —
routed by the landos.signal_type_registry (signal_class='discovery', mints=true),
never by hardcoded logic. This adds NO second writer and NEVER touches
public.acquisition_leads: sync_to_crm projects these on its next gated run, same
discovered-tier path as the recipe/taxsale mint. The recipe mint
(mint_recipe_leads.py) is unchanged — its owner_leads rows stay byte-identical.

HONESTY / RED STOPS:
  * govt is NEVER minted as a seller (vintage_flag='govt' excluded everywhere) —
    the vintage-capture law.
  * fuzzy owner-name assemblage stays behind a spot-check gate: same-owner-adjacent
    matches EXACT-normalized names only, and its fire prints a spot-check reminder.
  * dry-run counts NET-NEW (candidate not already in owner_leads), so re-mint = 0.
  * ON CONFLICT (tenant, county, apn) DO NOTHING — additive, idempotent.
"""
import argparse

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

APG_TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
PROVENANCE = "discovered_signal"

# asset_class from the roll's cheap facts (canonical six; honest 'unknown'). DLGF class
# 4xx = commercial (checked first so a big-acreage golf course reads 'commercial', not
# 'acreage'); 529/530 residential; >=10 ac acreage.
ASSET_CLASS_EXPR = """
    case when po.property_class ~ '^4[0-9][0-9]$' then 'commercial'
         when coalesce(po.acres,0) >= 10 then 'acreage'
         when po.property_class in ('529','530') then 'residential'
         else 'unknown' end
"""

# owner_type from the recipe's entity sweep (owner_kind), honest NULL otherwise.
OWNER_TYPE_EXPR = """
    case when po.owner_kind = 'entity' then 'entity'
         when po.owner_kind = 'human'  then 'individual' end
"""

# Each DISCOVERY source: the candidate predicate over the (non-govt) owner roll.
# lens is read from the registry at run time (single source of truth).
MINE_SOURCES = {
    "mine_absentee": "po.owner_mailing_state is not null and po.owner_mailing_state "
                     "<> (case left(po.county_fips,2) when '18' then 'IN' when '26' then 'MI' end)",
    "mine_entity": "po.owner_kind = 'entity'",
    "mine_exempt": "po.owner_name ~* "
                   "'\\y(church|chapel|parish|ministr|congregation|temple|mosque|synagogue|diocese|"
                   "tabernacle|worship|lodge|elks|moose|masonic|odd ?fellows|knights of|shriner|"
                   "eagles aerie|vfw|veterans of foreign|american legion|amvets|legion post|grange)\\y'",
    "mine_same_owner_adjacent": "exists (select 1 from landos.v_mine_same_owner_adjacent v "
                                "where v.county_fips = po.county_fips and v.parcel_apn = po.parcel_apn)",
    # UNBLOCKED by the assessor widen (mig 030) + Vanderburgh's roll (year_built + assessed +
    # real DLGF class codes). Both lens=CONVERSION, HELD per the ruling (mint=true = counted).
    "mine_teardown": "po.year_built between 1 and 1960 and po.improvement_assessed > 0 "
                     "and po.land_assessed > 0 and po.improvement_assessed < 0.5 * po.land_assessed",
    "mine_class_play": "po.property_class in ('410','411','416','462','463','481')",
}


def _candidate_sql(source: str, predicate: str) -> str:
    """owner_leads-shaped candidate rows for a mine source (govt excluded)."""
    return f"""
        select po.county_fips, po.parcel_apn, po.owner_name,
               po.situs_address,
               po.owner_mailing as owner_mailing_address,
               po.owner_mailing_city, po.owner_mailing_state, po.owner_mailing_zip,
               ({OWNER_TYPE_EXPR}) as owner_type,
               ({ASSET_CLASS_EXPR}) as asset_class
        from landos.parcel_owner po
        where po.vintage_flag is distinct from 'govt'
          and ({predicate})
    """


NETNEW_COUNT = """
    select count(*) from ({cand}) c
    left join landos.owner_leads ol
      on ol.tenant_id = %(tenant)s and ol.county_fips = c.county_fips
     and ol.parcel_apn = c.parcel_apn
    where ol.lead_id is null
"""

# Ground-lease towers: the 84 ST_Contains-stamped signals (host-owner = registrant).
GROUND_LEASE_CAND = """
    select ds.county_fips, ds.parcel_apn,
           ds.raw_payload->>'registrant' as owner_name,
           ds.raw_payload->>'structure_address' as situs_address,
           ds.raw_payload->>'registrant_mail' as owner_mailing_address,
           null::text as owner_mailing_city, null::text as owner_mailing_state, null::text as owner_mailing_zip,
           'entity'::text as owner_type,
           'vacant_land'::text as asset_class
    from landos.discovery_signals ds
    where ds.source_type = 'ground_lease'
      and coalesce(ds.parcel_apn,'') <> ''
      and (ds.raw_payload->>'lead_is_host_parcel_owner') = 'true'
"""

GROUND_LEASE_NETNEW = """
    select count(*) from ({cand}) c
    left join landos.owner_leads ol
      on ol.tenant_id = %(tenant)s and ol.county_fips = c.county_fips and ol.parcel_apn = c.parcel_apn
    where ol.lead_id is null
""".format(cand=GROUND_LEASE_CAND)

# Flood (lens FLOOD, DISCOVERY): the persisted SFHA parcels (landos.parcel_flood,
# JOB 4) joined to the owner roll. LaPorte has owners; StJoe SFHA parcels are
# geometry-only -> owner-dark, an honest named gap until the StJoe roll loads.
FLOOD_NETNEW = """
    select count(*) from landos.parcel_flood pf
    join landos.parcel_owner po
      on po.county_fips = pf.county_fips and po.parcel_apn = pf.parcel_apn
     and po.vintage_flag is distinct from 'govt'
    left join landos.owner_leads ol
      on ol.tenant_id = %(tenant)s and ol.county_fips = pf.county_fips and ol.parcel_apn = pf.parcel_apn
    where ol.lead_id is null
"""


def registry_lens(cur):
    cur.execute("select source_type, lens, signal_class, mints from landos.signal_type_registry")
    return {r["source_type"]: r for r in cur.fetchall()}


def dry_run(sources=None):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '300s'")
        reg = registry_lens(cur)
        want = sources or (list(MINE_SOURCES) + ["ground_lease", "flood_remap"])
        per_lens = {}
        rows = []
        for src in want:
            meta = reg.get(src, {})
            lens = meta.get("lens", "?")
            if src in MINE_SOURCES:
                cur.execute(NETNEW_COUNT.format(cand=_candidate_sql(src, MINE_SOURCES[src])),
                            {"tenant": APG_TENANT})
            elif src == "ground_lease":
                cur.execute(GROUND_LEASE_NETNEW, {"tenant": APG_TENANT})
            elif src == "flood_remap":
                cur.execute(FLOOD_NETNEW, {"tenant": APG_TENANT})
            else:
                continue
            n = cur.fetchone()["count"]
            rows.append((src, lens, n))
            per_lens[lens] = per_lens.get(lens, 0) + n

    print(f"\n{'='*72}\nDISCOVERY MINT — DRY-RUN net-new counts (NO writes)\n{'='*72}")
    print(f"{'source':<28} {'lens':<20} net-new to book")
    for src, lens, n in rows:
        print(f"  {src:<26} {lens:<20} {n:>8}")
    print(f"\n  per-lens totals:")
    for lens, n in sorted(per_lens.items(), key=lambda x: -x[1]):
        print(f"    {lens:<22} {n:>8}")
    print(f"\n  NOTE: sources overlap (a parcel can be absentee AND entity); the mint is\n"
          f"  ON CONFLICT DO NOTHING so the FIRST source to mint a parcel wins. Run order\n"
          f"  determines lens attribution — set it deliberately per operator recipe.")
    print(f"\n  GATED operator fire (per source — spot-check the sample FIRST):")
    for src, lens, n in rows:
        gate = "  # SPOT-CHECK GATE: eyeball 10 rows before firing" if src == "mine_same_owner_adjacent" else ""
        print(f"    .venv/bin/python -m scripts.mint_discovery --source {src}{gate}")
    print(f"    .venv/bin/python -m scripts.sync_to_crm   # single writer -> acquisition_leads (discovered tier)")
    return rows


# --- GATED LIVE FIRE (owner_leads writes; NEVER touches public.acquisition_leads) -----
# Net-new candidate rows -> landos.owner_leads, provenance='discovered_signal',
# source='signal:<source_type>', raw_meta carrying the lens + signal_tags + asset_class.
# sync_to_crm (the single writer) projects them on its next run — same discovered tier as
# the recipe/taxsale mint. ON CONFLICT + left-join net-new => idempotent (re-fire mints 0).
FIRE_SQL = """
    insert into landos.owner_leads
        (tenant_id, county_fips, parcel_apn, owner_name, situs_address,
         owner_mailing_address, owner_mailing_city, owner_mailing_state, owner_mailing_zip,
         owner_type, discovery_seed, source, provenance, raw_meta)
    select %(tenant)s, c.county_fips, c.parcel_apn, c.owner_name, c.situs_address,
           c.owner_mailing_address, c.owner_mailing_city, c.owner_mailing_state, c.owner_mailing_zip,
           c.owner_type, false, %(source)s, %(provenance)s,
           jsonb_build_object(
             'source_type', %(source_type)s::text,
             'lens', %(lens)s::text,
             'signal_tags', jsonb_build_array(%(source_type)s::text, %(lens_lower)s::text),
             'asset_class', c.asset_class)
    from ({cand}) c
    left join landos.owner_leads ol
      on ol.tenant_id = %(tenant)s and ol.county_fips = c.county_fips and ol.parcel_apn = c.parcel_apn
    where ol.lead_id is null
    on conflict (tenant_id, county_fips, parcel_apn) do nothing
"""


def _cand_for(source):
    """The candidate SQL for a fireable discovery source (govt already excluded).
    flood_remap is a parcel_flood join of a different shape — not fired through this path."""
    if source in MINE_SOURCES:
        return _candidate_sql(source, MINE_SOURCES[source])
    if source == "ground_lease":
        return GROUND_LEASE_CAND
    return None


def spot_check(source, n=10):
    """Render N candidate rows for an eyeball BEFORE firing (the 624 gate pattern). For
    CONVERSION (mine_exempt) the false positives to hunt are a 'church' that is really a
    cemetery association or school district — though the govt vintage-flag already excludes
    both (GOVT_RE covers school/cemetery). No writes."""
    cand = _cand_for(source)
    if cand is None:
        raise SystemExit(f"no candidate SQL for {source!r} (flood/HOLD or unknown)")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '120s'")
        cur.execute(f"select * from ({cand}) c order by c.county_fips, c.parcel_apn limit %s", (n,))
        rows = cur.fetchall()
    print(f"\n{'='*74}\nSPOT-CHECK {source} — {len(rows)} candidate rows (eyeball for false positives)\n{'='*74}")
    for i, r in enumerate(rows, 1):
        print(f"{i:2}. {r['owner_name']!r}  [{r.get('owner_type')}]  asset={r.get('asset_class')}")
        print(f"     {r['county_fips']}/{r['parcel_apn']}  situs={r.get('situs_address')!r}  "
              f"mail={r.get('owner_mailing_city')},{r.get('owner_mailing_state')}")
    return rows


def fire(sources):
    """Live-fire: write net-new candidates to owner_leads for each source (registry-gated
    on mints=true). Prints per-source minted count + before/after census. Idempotent."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '300s'")
        reg = registry_lens(cur)
        results = []
        for src in sources:
            meta = reg.get(src, {})
            lens = meta.get("lens")
            if not meta.get("mints"):
                print(f"  SKIP {src}: registry mints=false (not a discovery mint source)"); continue
            cand = _cand_for(src)
            if cand is None:
                print(f"  SKIP {src}: no fire candidate SQL (flood/HOLD or unknown)"); continue
            cur.execute("select count(*) c from landos.owner_leads where tenant_id=%s and source=%s",
                        (APG_TENANT, f"signal:{src}"))
            before = cur.fetchone()["c"]
            cur.execute(FIRE_SQL.format(cand=cand),
                        {"tenant": APG_TENANT, "source": f"signal:{src}", "provenance": PROVENANCE,
                         "source_type": src, "lens": lens, "lens_lower": (lens or "").lower()})
            minted = cur.rowcount
            conn.commit()
            cur.execute("select count(*) c from landos.owner_leads where tenant_id=%s and source=%s",
                        (APG_TENANT, f"signal:{src}"))
            after = cur.fetchone()["c"]
            results.append((src, lens, minted, before, after))
            print(f"  FIRED {src} [{lens}]: minted={minted}  owner_leads[signal:{src}] {before}->{after}")
    print("\nNext: `python -m scripts.sync_to_crm` (single writer) projects these to "
          "public.acquisition_leads as status='discovered'.")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="lens-aware discovery mint (dry-run counts by default)")
    ap.add_argument("--source", action="append", help="a discovery source_type; repeatable")
    ap.add_argument("--all", action="store_true", help="all mine + ground_lease sources")
    ap.add_argument("--dry-run", action="store_true", help="count net-new; write nothing (default)")
    ap.add_argument("--spot-check", metavar="SOURCE", help="render N candidate rows for eyeball (no writes)")
    ap.add_argument("--n", type=int, default=10, help="spot-check sample size")
    ap.add_argument("--fire", action="store_true",
                    help="LIVE: write net-new candidates to landos.owner_leads (requires --source; gated)")
    a = ap.parse_args()
    if a.spot_check:
        spot_check(a.spot_check, a.n)
    elif a.fire:
        if not a.source:
            raise SystemExit("--fire requires explicit --source (no accidental --all fire)")
        fire(a.source)
    else:
        dry_run(None if a.all else a.source)
