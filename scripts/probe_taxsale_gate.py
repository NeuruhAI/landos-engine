"""
probe_taxsale_gate.py — PROVE the promote gate against the LIVE DB, then roll back.

A dry-run that never touches the DB can't prove the filter. This inserts a SYNTHETIC taxsale
signal in an isolated county (99999), then exercises the REAL query + insert from
scripts/mint_taxsale_leads (imported, not reimplemented):

  1. promoted=false -> the mint's SIGNALS query returns it 0 times (the gate holds)
  2. flip promoted=true -> SIGNALS returns it, and MINT inserts a lead with the owner joined
  3. ROLLBACK — the synthetic signal + probe lead never persist

    python -m scripts.probe_taxsale_gate      # prints PASS/FAIL, writes nothing (always rolls back)

Exit 0 iff both gate assertions hold.
"""
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.freshness import upcoming_taxsale
from scripts.mint_taxsale_leads import SIGNALS, MINT, APG_TENANT, classify_owner_type

PROBE_FIPS = "99999"           # isolated so only our synthetic is in scope
PROBE_APN = "PROBE-GATE-0001"
PROBE_OWNER = "DOE JANE Q"


def _candidates(cur):
    cur.execute(SIGNALS, {"only_fips": [PROBE_FIPS]})
    return cur.fetchall()


def run():
    future = date.today() + timedelta(days=45)
    rp = {"sale_date": future.isoformat(), "acreage": 1.5, "raw": {"PROPTYPE": "VACANT"}}
    ok = True
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            # seed a synthetic UNPROMOTED taxsale signal
            cur.execute("""
                insert into landos.discovery_signals
                    (source_type, source_ref, county_fips, parcel_apn, owner_name,
                     signal_date, signal_class, confidence, raw_payload, status, promoted)
                values ('taxsale', %s, %s, %s, %s, %s, 'temporal', 0.5, %s, 'new', false)
                returning signal_id""",
                (f"probe:{PROBE_APN}", PROBE_FIPS, PROBE_APN, PROBE_OWNER,
                 future, Jsonb(rp)))
            sig_id = cur.fetchone()["signal_id"]

            # (1) UNPROMOTED -> the gate must exclude it
            before = _candidates(cur)
            g1 = len(before) == 0
            print(f"  [1] unpromoted → mint candidates in {PROBE_FIPS}: {len(before)}  "
                  f"({'PASS' if g1 else 'FAIL — gate leaked'})")

            # (2) PROMOTE it -> now a candidate, and MINT inserts a lead with owner joined
            cur.execute("update landos.discovery_signals set promoted=true where signal_id=%s", (sig_id,))
            after = _candidates(cur)
            g2a = len(after) == 1 and after[0]["parcel_apn"] == PROBE_APN
            minted_owner = None
            if after:
                s = after[0]
                fresh_ok, why = upcoming_taxsale(s["signal_date"], s["raw_payload"], s["owner_name"], date.today())
                if fresh_ok:
                    cur.execute(MINT, {
                        "tenant_id": APG_TENANT, "county_fips": s["county_fips"],
                        "parcel_apn": s["parcel_apn"], "owner_name": s["owner_name"],
                        "land_use_class": "VACANT", "acreage": 1.5,
                        "owner_type": classify_owner_type(s["owner_name"]),
                        "source": "probe", "provenance": "probe", "confidence_score": 0.5,
                        "raw_meta": Jsonb(s["raw_payload"])})
                    row = cur.fetchone()
                    if row:
                        cur.execute("select owner_name from landos.owner_leads where lead_id=%s",
                                    (row["lead_id"],))
                        minted_owner = cur.fetchone()["owner_name"]
                else:
                    print(f"      (freshness rejected the synthetic: {why})")
            g2b = minted_owner == PROBE_OWNER
            print(f"  [2] promoted → candidates: {len(after)} ; minted lead owner: {minted_owner!r}  "
                  f"({'PASS' if (g2a and g2b) else 'FAIL'})")

            ok = g1 and g2a and g2b
        finally:
            conn.rollback()   # ALWAYS — the probe never persists
    print(f"\n{'✅ GATE PROVEN' if ok else '❌ GATE FAILED'} — probe rolled back, no writes.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
