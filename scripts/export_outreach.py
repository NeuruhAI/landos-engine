"""
export_outreach.py — produce the outreach-ready, mail-merge CSV for a county, with
skip-trace columns that Tracerfy will populate.

    python -m scripts.export_outreach --county berrien
    python -m scripts.export_outreach --county berrien --min-score 60 --limit 200

Output: data/outreach_<county>.csv with one row per owner lead (ranked by
final_lead_score desc):
    owner_name, mailing_* , situs_address, apn, final_lead_score,
    + skip-trace columns Tracerfy fills: phone, phone_secondary, email,
      secondary_mailing, skip_trace_status

SKIP TRACE IS GATED. Tracerfy is the chosen provider at $0.02/lead. The real batch
call is wired (tracerfy_batch_trace) but ONLY fires with --skip-trace, which is OFF
by default. With the flag off (default) the script prints exactly what a run WOULD
cost and traces nothing — no spend without an explicit go.
"""
import argparse
import csv
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from scripts.owner_leads import county_alias_to_fips, TENANT

TRACERFY_COST_PER_LEAD = 0.02  # USD, per Tracerfy pricing
TRACERFY_API_URL = "https://api.tracerfy.com/v1/skiptrace/batch"  # batch skip-trace endpoint

OUT_FIELDS = [
    "rank", "final_lead_score", "owner_name", "owner_type", "absentee_tier",
    "owner_mailing_address", "owner_mailing_city", "owner_mailing_state",
    "owner_mailing_zip", "situs_address", "apn", "county_fips", "land_use_class",
    "acreage", "signal_overlap",
    # --- columns Tracerfy populates (empty until traced) ---
    "phone", "phone_secondary", "email", "secondary_mailing", "skip_trace_status",
]


def load_ranked_leads(cur, county_fips: str, min_score: float, limit: Optional[int]):
    q = ("select * from landos.owner_leads where tenant_id=%s and county_fips=%s "
         "and coalesce(final_lead_score,0) >= %s order by final_lead_score desc nulls last")
    params: List[Any] = [TENANT, county_fips, min_score]
    if limit:
        q += " limit %s"
        params.append(limit)
    cur.execute(q, params)
    return cur.fetchall()


def tracerfy_batch_trace(leads: List[Dict[str, Any]], api_key: str) -> Dict[str, Dict[str, Any]]:
    """REAL Tracerfy batch skip-trace call. Returns {lead_id: {phone,email,...}}.

    GATED: only invoked when --skip-trace is passed. Each lead costs
    TRACERFY_COST_PER_LEAD. Never called on the default path."""
    payload = {
        "records": [
            {
                "id": str(lead["lead_id"]),
                "name": lead.get("owner_name"),
                "address": lead.get("owner_mailing_address"),
                "city": lead.get("owner_mailing_city"),
                "state": lead.get("owner_mailing_state"),
                "zip": lead.get("owner_mailing_zip"),
            }
            for lead in leads
        ]
    }
    req = urllib.request.Request(
        TRACERFY_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "landos-engine/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Tracerfy HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Tracerfy network error: {e.reason}")
    out: Dict[str, Dict[str, Any]] = {}
    for rec in data.get("results", []):
        out[str(rec.get("id"))] = {
            "phone": rec.get("phone"),
            "phone_secondary": rec.get("phone2") or rec.get("phone_secondary"),
            "email": rec.get("email"),
            "secondary_mailing": rec.get("secondary_address") or rec.get("secondary_mailing"),
            "raw": rec,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--skip-trace", action="store_true",
                    help="ACTUALLY call Tracerfy ($0.02/lead). OFF by default — no spend.")
    args = ap.parse_args()

    fips = county_alias_to_fips(args.county)
    if not fips:
        raise SystemExit(f"Unknown county {args.county!r}.")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            leads = load_ranked_leads(cur, fips, args.min_score, args.limit)

        to_trace = [l for l in leads if (l.get("skip_trace_status") or "not_traced") == "not_traced"]
        traced: Dict[str, Dict[str, Any]] = {}

        if args.skip_trace:
            api_key = os.environ.get("TRACERFY_API_KEY")
            if not api_key:
                raise SystemExit("TRACERFY_API_KEY not set — refusing to attempt a paid trace.")
            cost = len(to_trace) * TRACERFY_COST_PER_LEAD
            print(f"TRACERFY ENABLED — tracing {len(to_trace)} leads at "
                  f"${TRACERFY_COST_PER_LEAD:.2f} = ${cost:.2f}")
            traced = tracerfy_batch_trace(to_trace, api_key)
            with conn.cursor() as cur:
                for lead in to_trace:
                    res = traced.get(str(lead["lead_id"]))
                    if not res:
                        continue
                    cur.execute(
                        "update landos.owner_leads set skip_trace_status='traced', "
                        "skip_trace_meta=%s, updated_at=now() where lead_id=%s",
                        (Jsonb(res), lead["lead_id"]),
                    )
            conn.commit()
        else:
            cost = len(to_trace) * TRACERFY_COST_PER_LEAD
            print(f"TRACERFY DISABLED — would trace {len(to_trace)} leads at "
                  f"${TRACERFY_COST_PER_LEAD:.2f} = ${cost:.2f}, awaiting go")

    out = args.out or os.path.join(
        os.path.expanduser("~/Documents/landos-engine/data"),
        f"outreach_{args.county.lower()}.csv",
    )
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for i, lead in enumerate(leads, 1):
            ov = lead.get("signal_overlap") or {}
            if isinstance(ov, str):
                ov = json.loads(ov)
            tr = traced.get(str(lead["lead_id"]), {})
            w.writerow({
                "rank": i,
                "final_lead_score": lead.get("final_lead_score"),
                "owner_name": lead.get("owner_name"),
                "owner_type": lead.get("owner_type"),
                "absentee_tier": lead.get("absentee_tier"),
                "owner_mailing_address": lead.get("owner_mailing_address"),
                "owner_mailing_city": lead.get("owner_mailing_city"),
                "owner_mailing_state": lead.get("owner_mailing_state"),
                "owner_mailing_zip": lead.get("owner_mailing_zip"),
                "situs_address": lead.get("situs_address"),
                "apn": lead.get("parcel_apn"),
                "county_fips": lead.get("county_fips"),
                "land_use_class": lead.get("land_use_class"),
                "acreage": lead.get("acreage"),
                "signal_overlap": "|".join(ov.get("distress_stack") or [])
                                  + ("|OZ" if ov.get("in_opportunity_zone") else "") or "none",
                "phone": tr.get("phone", ""),
                "phone_secondary": tr.get("phone_secondary", ""),
                "email": tr.get("email", ""),
                "secondary_mailing": tr.get("secondary_mailing", ""),
                "skip_trace_status": ("traced" if tr else (lead.get("skip_trace_status") or "not_traced")),
            })
    print(f"Mail-merge CSV: {out}  ({len(leads)} leads)")


if __name__ == "__main__":
    main()
