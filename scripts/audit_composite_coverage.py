"""
audit_composite_coverage.py — READ-ONLY audit for the composite-scorer BATCH path.

P0 of "light up landos.scores for the 136 production leads". Writes NOTHING
(connection is forced read-only). Answers, against live lvdi:

  1. SCHEMA   — landos.scores / identity_nodes / scoring_weights / comps /
                event_ledger / audit_log columns + nullability. Decides whether a
                MISSING axis can be stored as NULL (first-class) or needs a column.
  2. THE 136  — resolve each qualified owner_lead to its identity_node
                (tenant_id, parcel_apn). How many HAVE a node, how many NEED one,
                how many already carry ANY score.
  3. AXES     — per-axis input availability over the 136 (owner / distress /
                arbitrage), grounded vs MISSING. MISSING is first-class, never 0.
  4. WEIGHTS  — is there an active 'default'-profile scoring_weights row for the
                tenant, or does the scorer fall back to DEFAULT_WEIGHTS (v0)?

    python -m scripts.audit_composite_coverage
"""
import json
import sys

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def hr(title: str) -> None:
    print(f"\n{'='*72}\n{title}\n{'='*72}")


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True  # HARD guarantee: this audit cannot write.
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")

            # ---- 1. SCHEMA -------------------------------------------------
            hr("1. SCHEMA — core landos tables (column · type · nullable · default)")
            cur.execute(
                """
                select table_name, column_name, data_type, is_nullable, column_default
                from information_schema.columns
                where table_schema = 'landos'
                  and table_name in ('scores','identity_nodes','scoring_weights',
                                     'comps','event_ledger','audit_log')
                order by table_name, ordinal_position
                """
            )
            cols = cur.fetchall()
            if not cols:
                print("  (no matching tables found in schema 'landos')")
            current = None
            for c in cols:
                if c["table_name"] != current:
                    current = c["table_name"]
                    print(f"\n  landos.{current}")
                dflt = (c["column_default"] or "")[:32]
                print(f"    {c['column_name']:<22} {c['data_type']:<26} "
                      f"null={c['is_nullable']:<3} {dflt}")
            present = {c["table_name"] for c in cols}
            for t in ("scores", "identity_nodes", "scoring_weights", "comps",
                      "event_ledger", "audit_log"):
                if t not in present:
                    print(f"  !! landos.{t} NOT FOUND")

            # ---- 2. THE 136 — node + score resolution ----------------------
            hr("2. THE 136 — identity_node + score resolution")
            cur.execute(
                "select count(*) n from landos.owner_leads where tenant_id=%s",
                (TENANT,))
            raw = cur.fetchone()["n"]
            cur.execute(
                "select count(*) n from landos.owner_leads "
                "where tenant_id=%s and not discovery_seed", (TENANT,))
            qualified = cur.fetchone()["n"]
            print(f"  raw owner_leads (tenant):        {raw}")
            print(f"  qualified (not discovery_seed):  {qualified}   (the 136)")

            # nodes that exist for the qualified parcels (by tenant+parcel_apn)
            cur.execute(
                """
                with q as (
                  select lead_id, parcel_apn
                  from landos.owner_leads
                  where tenant_id=%s and not discovery_seed
                )
                select
                  count(*)                                            as leads,
                  count(distinct q.parcel_apn)                        as distinct_apn,
                  count(distinct q.parcel_apn) filter (where n.id is not null) as apn_with_node,
                  count(n.id)                                         as node_rows_matched
                from q
                left join landos.identity_nodes n
                  on n.tenant_id=%s and n.parcel_apn=q.parcel_apn
                """,
                (TENANT, TENANT))
            r = cur.fetchone()
            print(f"  distinct parcel_apn in the set:  {r['distinct_apn']}")
            print(f"  parcels WITH an identity_node:   {r['apn_with_node']}")
            print(f"  parcels NEEDING a node created:  {r['distinct_apn'] - r['apn_with_node']}")
            print(f"  identity_node rows matched:      {r['node_rows_matched']} "
                  f"(>{r['apn_with_node']} ⇒ owner_hash variants per parcel)")

            # how many of the 136 already carry ANY score (latest-per-node)
            cur.execute(
                """
                with q as (
                  select parcel_apn from landos.owner_leads
                  where tenant_id=%s and not discovery_seed
                )
                select count(distinct q.parcel_apn) as apn_with_score
                from q
                join landos.identity_nodes n
                  on n.tenant_id=%s and n.parcel_apn=q.parcel_apn
                join landos.scores s on s.identity_node_id = n.id
                """,
                (TENANT, TENANT))
            print(f"  parcels with ANY existing score: {cur.fetchone()['apn_with_score']} "
                  f"(FINDING expects 0)")

            # ---- 3a. OWNER axis -------------------------------------------
            hr("3a. OWNER axis — owner_leads (absentee + type + succession)")
            cur.execute(
                """
                with q as (
                  select * from landos.owner_leads
                  where tenant_id=%s and not discovery_seed
                )
                select
                  count(*) total,
                  count(absentee_score)                       as absentee_score_present,
                  count(*) filter (where absentee_tier is not null
                                     and absentee_tier <> 'unknown') as absentee_tier_known,
                  count(*) filter (where absentee_tier='out_of_state')  as out_of_state,
                  count(*) filter (where absentee_flag)               as absentee_flag_true,
                  count(*) filter (where owner_type is not null)      as owner_type_known,
                  count(*) filter (where owner_type='entity')         as is_entity,
                  count(*) filter (where owner_type='individual')     as is_individual,
                  count(*) filter (where owner_type='govt')           as is_govt,
                  count(*) filter (where skip_trace_status in ('traced_web','traced_paid')) as traced,
                  count(*) filter (where (skip_trace_meta->>'deceased')::boolean is true) as deceased_flag,
                  count(*) filter (where skip_trace_meta ? 'age'
                                      or skip_trace_meta ? 'dob'
                                      or skip_trace_meta ? 'birth_year') as has_age_source
                from q
                """,
                (TENANT,))
            o = cur.fetchone()
            print(f"  total:                      {o['total']}")
            print(f"  absentee_tier known:        {o['absentee_tier_known']}  "
                  f"(out_of_state={o['out_of_state']}, flag_true={o['absentee_flag_true']})")
            print(f"  absentee_score present:     {o['absentee_score_present']}")
            print(f"  owner_type known:           {o['owner_type_known']}  "
                  f"(entity={o['is_entity']}, individual={o['is_individual']}, govt={o['is_govt']})")
            print(f"  skip-traced (web/paid):     {o['traced']}")
            print(f"  → age_band source present:  {o['has_age_source']}   "
                  f"(age/dob/birth_year in skip_trace_meta)")
            print(f"  → no_succession (deceased): {o['deceased_flag']}")
            # absentee_tier distribution
            cur.execute(
                """select coalesce(absentee_tier,'NULL') tier, count(*) n
                   from landos.owner_leads where tenant_id=%s and not discovery_seed
                   group by 1 order by 2 desc""", (TENANT,))
            print("  absentee_tier distribution: " +
                  ", ".join(f"{x['tier']}={x['n']}" for x in cur.fetchall()))

            # contact_relations heir edges for the 136
            cur.execute(
                """
                with q as (select lead_id from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select count(distinct cr.owner_lead_id) leads_with_relations,
                       count(*) filter (where cr.is_heir) heir_edges
                from landos.contact_relations cr join q on q.lead_id=cr.owner_lead_id
                """, (TENANT,))
            cr = cur.fetchone()
            print(f"  contact_relations: leads_with_edges={cr['leads_with_relations']}, "
                  f"heir_edges={cr['heir_edges']}")

            # ---- 3b. DISTRESS axis ----------------------------------------
            hr("3b. DISTRESS axis — discovery_signals + owner_leads.signal_overlap")
            cur.execute(
                """
                with q as (select lead_id from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select count(distinct ds.matched_lead_id) leads_matched,
                       count(*) total_signal_rows
                from landos.discovery_signals ds join q on q.lead_id=ds.matched_lead_id
                """, (TENANT,))
            d = cur.fetchone()
            print(f"  discovery_signals matched to the 136: "
                  f"leads={d['leads_matched']}, signal_rows={d['total_signal_rows']}")
            cur.execute(
                """
                with q as (select lead_id from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select ds.source_type, count(*) n,
                       count(distinct ds.matched_lead_id) leads
                from landos.discovery_signals ds join q on q.lead_id=ds.matched_lead_id
                group by 1 order by 2 desc
                """, (TENANT,))
            rows = cur.fetchall()
            if rows:
                print("  by source_type (matched only):")
                for x in rows:
                    print(f"    {x['source_type']:<22} rows={x['n']} leads={x['leads']}")
            else:
                print("  by source_type: (none matched to the 136)")
            # discovery_signals total table size + status
            cur.execute("select count(*) n from landos.discovery_signals")
            print(f"  discovery_signals TABLE total rows: {cur.fetchone()['n']}")

            # signal_overlap — a second distress source on owner_leads itself
            cur.execute(
                """
                with q as (select signal_overlap from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select
                  count(*) filter (where signal_overlap is not null
                                     and signal_overlap <> '{}'::jsonb
                                     and signal_overlap <> '[]'::jsonb) as nonempty
                from q
                """, (TENANT,))
            print(f"  owner_leads.signal_overlap non-empty: "
                  f"{cur.fetchone()['nonempty']} / {qualified}")
            # sample a few non-empty signal_overlap payloads to learn the shape
            cur.execute(
                """select signal_overlap from landos.owner_leads
                   where tenant_id=%s and not discovery_seed
                     and signal_overlap is not null
                     and signal_overlap <> '{}'::jsonb and signal_overlap <> '[]'::jsonb
                   limit 3""", (TENANT,))
            for x in cur.fetchall():
                print(f"    signal_overlap sample: {json.dumps(x['signal_overlap'])[:160]}")

            # signal_overlap is the REAL distress source (136/136). Dump its
            # vocabulary so the distress→composite-key mapping is grounded, and
            # so Bryan can see exactly which signals exist vs which the model wants.
            cur.execute(
                """
                with q as (select signal_overlap so from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select
                  count(*) filter (where so ? 'distress_stack')                       has_stack_key,
                  count(*) filter (where jsonb_array_length(
                                     coalesce(so->'distress_stack','[]'::jsonb)) > 0)  nonempty_stack,
                  count(*) filter (where so ? 'signals')                              has_signals_key
                from q
                """, (TENANT,))
            so = cur.fetchone()
            print(f"  signal_overlap.distress_stack present/non-empty: "
                  f"{so['has_stack_key']}/{so['nonempty_stack']}; signals key: {so['has_signals_key']}")
            cur.execute(
                """
                with q as (select signal_overlap so from landos.owner_leads
                           where tenant_id=%s and not discovery_seed),
                toks as (
                  select jsonb_array_elements_text(
                           coalesce(so->'distress_stack', so->'signals', '[]'::jsonb)) tok
                  from q)
                select tok, count(*) n from toks group by 1 order by 2 desc
                """, (TENANT,))
            vocab = cur.fetchall()
            print("  signal_overlap distress vocabulary (token → leads):")
            for v in vocab:
                print(f"    {v['tok']:<26} {v['n']}")
            # all top-level keys present in the signal_overlap object
            cur.execute(
                """
                with q as (select signal_overlap so from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select distinct jsonb_object_keys(so) k from q order by 1
                """, (TENANT,))
            print("  signal_overlap object keys: " +
                  ", ".join(k['k'] for k in cur.fetchall()))

            # ---- 3c. ARBITRAGE axis ---------------------------------------
            hr("3c. ARBITRAGE axis — landos.comps + price sources")
            try:
                cur.execute("select count(*) n from landos.comps")
                ncomps = cur.fetchone()["n"]
                print(f"  landos.comps TABLE total rows: {ncomps}  "
                      f"(FINDING expects 0 → arbitrage hard-absent)")
            except Exception as e:
                print(f"  landos.comps: {e.__class__.__name__}: {e}")
            # any per-parcel price columns on owner_leads? (acreage is the only arb input present)
            cur.execute(
                """
                with q as (select * from landos.owner_leads
                           where tenant_id=%s and not discovery_seed)
                select count(acreage) acreage_present, count(*) total from q
                """, (TENANT,))
            a = cur.fetchone()
            print(f"  owner_leads.acreage present: {a['acreage_present']}/{a['total']} "
                  f"(acreage is the ONLY arbitrage input available; price/comps absent)")

            # ---- 4. WEIGHTS -----------------------------------------------
            hr("4. WEIGHTS — active scoring_weights for tenant/profile")
            try:
                cur.execute(
                    """select profile, version, active
                       from landos.scoring_weights where tenant_id=%s
                       order by profile, version""", (TENANT,))
                ws = cur.fetchall()
                if ws:
                    for w in ws:
                        print(f"  profile={w['profile']:<16} version={w['version']} "
                              f"active={w['active']}")
                else:
                    print("  (no scoring_weights rows for tenant ⇒ scorer falls back "
                          "to DEFAULT_WEIGHTS, weights_version=0)")
            except Exception as e:
                print(f"  scoring_weights: {e.__class__.__name__}: {e}")

            print("\nAUDIT COMPLETE (read-only). No rows written.\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface DB/connection errors plainly
        print(f"AUDIT FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        sys.exit(1)
