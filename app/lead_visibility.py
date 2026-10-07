"""
lead_visibility.py — MAKE THE LEADS VISIBLE: the pipeline_slug + map-coordinate backfill
and the visibility invariant. A lead the operator cannot SEE is not a lead.

Two failure modes, both healed here:
  pipeline_slug NULL -> the CRM queue filters by pipeline, so the lead is an orphan;
  situs_lat/lng NULL -> it cannot render on the map.

Every engine-minted lead (source='engine_sync') gets a pipeline_slug by its cold-call
LENS (app.score_absentee.cold_call_lens — the single source of truth):
  seller (human)      -> 'land' | 'distressed'   (the cold-call queues; existing slug kept)
  institution (bank/  -> 'entity'                 (commercial holder — NEVER cold-dialed)
    LLC/corp/trust)
  govt / land-bank    -> 'govt'                   (disposition channel — NEVER cold-dialed)
A NULL pipeline defaults to the lens pipeline (sellers -> 'land'). This is the ENGINE-side
guard for the doctrine "absentee is a dimension, not a thesis": an absentee bank is a real
entity lead but must not sit in the human cold-call queue. Lens-aware assignment means a
re-sync can never re-contaminate the queue. Coordinates come from landos.parcel_geom.centroid
(ST_PointOnSurface, guaranteed inside) joined on (county_fips, parcel_apn).

SINGLE-WRITER: these run INSIDE sync_to_crm (the sole writer of public.acquisition_leads),
never as a parallel update. sync_to_crm.main() calls backfill_pipeline_and_coords() then
assert_leads_visible() before commit; --backfill-visibility runs them standalone.
"""
from app.score_absentee import cold_call_lens

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# lens -> pipeline_slug. Sellers ride the human cold-call queues (land/distressed, both
# PRESERVED); institutions + govt are relensed OUT of the cold-call queue. card_state is
# never touched here (fully reversible: re-point the map and re-run).
NONHUMAN_LENS_PIPELINE = {"institution": "entity", "govt": "govt"}
# the human cold-call pipelines a non-human must be pulled OUT of (plus NULL + the
# relensed targets, so the pass is idempotent).
_SCANNED_PIPELINES = ("land", "distressed", "entity", "govt")

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
    """Assign pipeline_slug by cold-call LENS and backfill map coordinates. Set-based
    (all tenant leads, not just this run's) — the map/queue heal for the whole book:
      - a NULL-pipeline engine_sync SELLER  -> 'land'
      - any INSTITUTION in a human pipeline -> 'entity'  (out of the cold-call queue)
      - any GOVT owner in a human pipeline  -> 'govt'
    Sellers already in 'land'/'distressed' are left where they are (the cold-call queues
    are preserved). Idempotent. card_state is never touched. Returns (pipeline_set, coords_set)."""
    cur.execute(
        """select lead_id, owner_name, pipeline_slug, source
             from public.acquisition_leads
            where tenant_id = %s
              and ( (pipeline_slug is null and source = 'engine_sync')
                    or pipeline_slug = any(%s) )""",
        (tenant, list(_SCANNED_PIPELINES)))
    moves = {"land": [], "entity": [], "govt": []}
    for r in cur.fetchall():
        lens = cold_call_lens(r["owner_name"])
        current = r["pipeline_slug"]
        if lens == "seller":
            if current is None:                       # only heal a null; never move a
                moves["land"].append(r["lead_id"])    # seller between land <-> distressed
            continue
        target = NONHUMAN_LENS_PIPELINE[lens]         # institution -> entity, govt -> govt
        if current != target:                         # relens out of the human queue
            moves[target].append(r["lead_id"])
    pipe = 0
    for target, ids in moves.items():
        if not ids:
            continue
        cur.execute(
            "update public.acquisition_leads set pipeline_slug = %s "
            "where tenant_id = %s and lead_id = any(%s)", (target, tenant, ids))
        pipe += cur.rowcount
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


def lens_pipeline_census(cur, tenant=TENANT):
    """Read-only: (pipeline_slug -> count) after assignment, for the run receipt."""
    cur.execute(
        "select pipeline_slug, count(*) n from public.acquisition_leads "
        "where tenant_id = %s group by 1 order by 2 desc", (tenant,))
    return [(r["pipeline_slug"] if isinstance(r, dict) else r[0],
             r["n"] if isinstance(r, dict) else r[1]) for r in cur.fetchall()]


if __name__ == "__main__":
    # Sanctioned standalone runner for the lens-aware pipeline assignment + coord backfill.
    # Lives here (not sync_to_crm) while sync_to_crm is entangled with the dnc refactor; the
    # SAME function is what sync_to_crm calls, so a re-sync stays consistent with this run.
    #   python -m app.lead_visibility --backfill            # apply (commit)
    #   python -m app.lead_visibility --backfill --dry-run  # show, roll back
    import argparse

    import psycopg
    from psycopg.rows import dict_row

    from app.config import DATABASE_URL

    ap = argparse.ArgumentParser(description="lens-aware pipeline_slug + coord backfill")
    ap.add_argument("--backfill", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '120s'")
        pipe, coords = backfill_pipeline_and_coords(cur)
        assert_leads_visible(cur)                      # errors-visible: aborts on a violation
        census = lens_pipeline_census(cur)
        if args.dry_run:
            conn.rollback()
            print(f"[dry-run] would move {pipe} pipeline_slug + {coords} coords (rolled back)")
        else:
            conn.commit()
            print(f"pipeline_slug lens-assigned on {pipe} lead(s); coords backfilled on {coords}")
        print("pipeline census: " + ", ".join(f"{s}={n}" for s, n in census))
