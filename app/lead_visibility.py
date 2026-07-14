"""
lead_visibility.py — MAKE THE LEADS VISIBLE: the pipeline_slug + map-coordinate backfill
and the visibility invariant. A lead the operator cannot SEE is not a lead.

Two failure modes, both healed here:
  pipeline_slug NULL -> the CRM queue filters by pipeline, so the lead is an orphan;
  situs_lat/lng NULL -> it cannot render on the map.

Every engine-minted lead (source='engine_sync') is an APG LAND play (lakefront parcels,
churches on acreage, dirt under towers) -> pipeline_slug='land' (an EXISTING slug; the lens
stays distinguishable via signal_tags, which the queue filters on). Coordinates come from
landos.parcel_geom.centroid (ST_PointOnSurface, guaranteed inside) joined on
(county_fips, parcel_apn) — the same zero-fuzz key the geometry load uses.

SINGLE-WRITER: these run INSIDE sync_to_crm (the sole writer of public.acquisition_leads),
never as a parallel update. sync_to_crm.main() calls backfill_pipeline_and_coords() then
assert_leads_visible() before commit; --backfill-visibility runs them standalone.
"""

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

PIPELINE_BACKFILL_SQL = """
    update public.acquisition_leads
       set pipeline_slug = 'land'
     where tenant_id = %s and pipeline_slug is null and source = 'engine_sync'
"""

COORDS_BACKFILL_SQL = """
    update public.acquisition_leads a
       set situs_lat = ST_Y(g.centroid), situs_lng = ST_X(g.centroid)
      from landos.parcel_geom g
     where a.tenant_id = %s
       and a.county_fips = g.county_fips
       and a.parcel_apn = g.parcel_apn
       and (a.situs_lat is null or a.situs_lng is null)
       and g.centroid is not null
"""

# The invariant's negative: an engine lead with no pipeline_slug, OR any lead missing
# coordinates WHILE a centroid exists for its parcel (an honest geometry-dark county is
# excluded — no centroid, nothing to backfill, not a bug).
LEADS_INVISIBLE = """
    select a.lead_id, a.owner_name
      from public.acquisition_leads a
     where a.tenant_id = %s
       and (
         (a.pipeline_slug is null and a.source = 'engine_sync')
         or ((a.situs_lat is null or a.situs_lng is null)
             and exists (select 1 from landos.parcel_geom g
                          where g.county_fips = a.county_fips and g.parcel_apn = a.parcel_apn
                            and g.centroid is not null))
       )
"""


def backfill_pipeline_and_coords(cur, tenant=TENANT):
    """Set pipeline_slug='land' on the engine's slug-less leads and backfill coordinates
    from the parcel centroid. Set-based (all tenant leads, not just this run's) — the
    map/queue heal for the whole book. Returns (pipeline_set, coords_set)."""
    cur.execute(PIPELINE_BACKFILL_SQL, (tenant,))
    pipe = cur.rowcount
    cur.execute(COORDS_BACKFILL_SQL, (tenant,))
    coords = cur.rowcount
    return pipe, coords


def assert_leads_visible(cur, tenant=TENANT):
    """Errors-visible doctrine: raise LOUDLY before commit if any lead is invisible — an
    engine lead with no pipeline_slug, or a lead lacking coordinates though its parcel HAS a
    centroid. Runs inside the sync txn, so a violation aborts (rolls back) the run. Honest
    geometry-dark counties (no centroid) are not violations — nothing to render from."""
    cur.execute(LEADS_INVISIBLE, (tenant,))
    bad = cur.fetchall()
    if bad:
        def _name(b):
            return (b.get("owner_name") if isinstance(b, dict) else b[1])
        def _id(b):
            return (b.get("lead_id") if isinstance(b, dict) else b[0])
        sample = ", ".join(f"{_id(b)} ({_name(b)!r})" for b in bad[:5])
        raise AssertionError(
            f"INVISIBLE LEADS — {len(bad)} lead(s) the operator cannot see "
            f"(no pipeline_slug and/or no map coordinates where geometry exists): "
            f"{sample}{' …' if len(bad) > 5 else ''}. The backfill failed to heal them; "
            f"aborting sync.")


# ---- EXACT re-apply into scripts/sync_to_crm.py -------------------------------------
# (kept here verbatim because sync_to_crm.py is currently entangled with a peer's
# uncommitted dnc refactor; this is the full, sufficient reconstruction of the wiring.)
#
# 1) import at top:
#       from app.lead_visibility import backfill_pipeline_and_coords, assert_leads_visible
#
# 2) in main()'s non-dry-run commit path, right after `contact_stats = sync_seller_contacts(cur)`
#    and before `conn.commit()`:
#       pipe_n, coord_n = backfill_pipeline_and_coords(cur)
#       assert_no_orphan_callable_leads(cur)
#       assert_leads_visible(cur)
#       # ... conn.commit() ...
#       print(f"visibility: pipeline_slug='land' set on {pipe_n}, coords backfilled on {coord_n}")
#
# 3) standalone CLI (mirrors --backfill-contacts): a `backfill_visibility(dry_run)` that
#    runs backfill_pipeline_and_coords() + assert_leads_visible() in its own txn, wired to
#    an `--backfill-visibility` argparse flag.
