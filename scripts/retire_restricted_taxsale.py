"""
retire_restricted_taxsale.py — retire the tax-sale.info leads that came through the UNGATED
mint (before mig 036), reversibly and with an audit trail.

    python -m scripts.retire_restricted_taxsale               # dry-run: audit + counts, no write
    python -m scripts.retire_restricted_taxsale --apply       # retire (reversible)

BACKGROUND: mint_taxsale_leads used to mint every taxsale signal with no promote gate. That put
tax-sale.info-sourced former-owner parcels into the live book — the exact ToU exposure (automated
aggregation for outreach). Williamson TX (MVBA county PDF) and any HUMAN-PROMOTED row are CLEAN
and KEPT. The restricted set is retired: card_state='dead', stage='archived', with a durable note
+ parcel_meta.retired — REVERSIBLE (flip card_state back), never deleted (AI-never-deletes).

Classification is by PROVENANCE, not guessed: a signal is restricted iff
raw_payload.source_page LIKE '%tax-sale.info%' AND it was not human-promoted.
"""
import argparse
from datetime import date

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# restricted lead_ids: tax-sale.info signal -> its matched owner_lead -> the CRM lead.
RESTRICTED_LEADS = """
    with restricted_signal as (
        select matched_lead_id
          from landos.discovery_signals
         where source_type = 'taxsale'
           and raw_payload->>'source_page' ilike '%%tax-sale.info%%'
           and promoted = false
           and matched_lead_id is not null
    )
    select a.lead_id
      from public.acquisition_leads a
      join restricted_signal r on r.matched_lead_id = a.source_lead_id
     where a.tenant_id = %(t)s and a.card_state <> 'dead'
"""

AUDIT = """
    select
      (select count(*) from landos.discovery_signals
        where source_type='taxsale' and raw_payload->>'source_page' ilike '%%tax-sale.info%%') restricted_signals,
      (select count(*) from landos.discovery_signals
        where source_type='taxsale' and source_url ilike '%%mvbalaw%%') clean_williamson,
      (select count(*) from landos.discovery_signals
        where source_type='taxsale' and promoted=true) human_promoted
"""

RETIRE = """
    update public.acquisition_leads a set
        card_state = 'dead',
        stage = 'archived',
        value_add_notes = trim(both E'\\n' from
            coalesce(value_add_notes || E'\\n', '') ||
            'RETIRED taxsale-tou ' || %(date)s::text ||
            ': restricted source tax-sale.info, no human promote (ungated mint before mig 036) '
            '— reversible, card_state can be restored on human promote'),
        parcel_meta = coalesce(parcel_meta, '{}'::jsonb) || jsonb_build_object(
            'retired', jsonb_build_object(
                'reason', 'taxsale_tou_restricted', 'source', 'tax-sale.info',
                'at', %(date)s, 'reversible', true, 'gate', 'mig_036_promote'))
      where a.tenant_id = %(t)s and a.lead_id = any(%(ids)s::uuid[]) and a.card_state <> 'dead'
    returning lead_id
"""

# engine-side annotation so owner_leads carries the same audit crumb (never deleted).
STAMP_OWNER = """
    update landos.owner_leads o set
        raw_meta = coalesce(raw_meta, '{}'::jsonb) || jsonb_build_object(
            'retired_tou', jsonb_build_object('source','tax-sale.info','at',%(date)s::text,'reversible',true))
      from public.acquisition_leads a
     where a.source_lead_id = o.lead_id and a.tenant_id = %(t)s and a.lead_id = any(%(ids)s::uuid[])
"""


def run(apply: bool):
    today = date.today().isoformat()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '180s'")
        cur.execute(AUDIT)
        a = cur.fetchone()
        print("=== TAX-SALE PROVENANCE AUDIT ===")
        print(f"  restricted (tax-sale.info) signals : {a['restricted_signals']}")
        print(f"  clean (Williamson TX / MVBA)       : {a['clean_williamson']}  (KEPT)")
        print(f"  human-promoted                     : {a['human_promoted']}  (KEPT)")

        cur.execute(RESTRICTED_LEADS, {"t": TENANT})
        ids = [r["lead_id"] for r in cur.fetchall()]
        print(f"\n  restricted CRM leads still active/not-dead: {len(ids)}  <- retire target")

        if not apply:
            conn.rollback()
            print("\nDRY-RUN — no writes. Re-run with --apply to retire (reversible).")
            return {"restricted": a["restricted_signals"], "clean": a["clean_williamson"],
                    "to_retire": len(ids), "retired": 0}

        cur.execute(RETIRE, {"t": TENANT, "ids": ids, "date": today})
        retired = cur.rowcount
        cur.execute(STAMP_OWNER, {"t": TENANT, "ids": ids, "date": today})
        stamped = cur.rowcount
        conn.commit()
        print(f"\nRETIRED {retired} CRM leads (card_state='dead', reversible); "
              f"stamped {stamped} owner_leads. Williamson + any promoted rows untouched.")
        return {"restricted": a["restricted_signals"], "clean": a["clean_williamson"],
                "retired": retired, "stamped": stamped}


def main():
    ap = argparse.ArgumentParser(description="retire ungated tax-sale.info leads (reversible)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run audit)")
    run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
