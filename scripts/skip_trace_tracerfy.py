"""
skip_trace_tracerfy.py — CONTACTABILITY via Tracerfy skip-trace (AXON, additive). THE live vendor.

    python -m scripts.skip_trace_tracerfy [--limit N] [--provenance P]
                                          [--from-export FILE.csv] [--confirm-spend] [--dry-run]

Vendor is locked to Tracerfy ($0.02/match, real API, NO minimum — misses are free). This is the
skip-trace path the scheduler calls; scripts/skip_trace_batchdata.py stays dormant in the repo.

It writes through the EXISTING contact rail (no second writer):

    Tracerfy  ->  resolved CSV (import_resolved schema)  ->  scripts.import_resolved.run()
              ->  landos.owner_leads.skip_trace_meta  ->  sync_to_crm  ->  acquisition_leads

So the DNC gate (sync suppresses do_not_call at the write boundary), the callable/confidence gate,
idempotency, and the audit manifest are all inherited. Phones are stamped source='tracerfy',
verified_via='tracerfy'. A phone Tracerfy flags DNC sets dnc_status='do_not_call'.

TARGETS = the universe export_skiptrace_targets.py already routes to Tracerfy: tenant, not
discovery_seed, owner_type='individual' (real people — entities are a separate pierce), a usable
name, a mailing OR situs address, skip_trace_status='not_traced'. --provenance/--limit scope a batch.

MODES (gate-in-data — never fabricate, never spend without a confirmed credential):
  1. --from-export FILE.csv : ingest a Tracerfy export (matched to targets by owner name). $0 here.
  2. TRACERFY_API_KEY set    : call the API; spend APPROVED but capped — over $BATCH_CAP the run
                               STOPS unless --confirm-spend. RUN IS BLOCKED ON THE KEY (provided
                               later) — this is built now, armed later.
  3. neither                 : NO-SPEND. Writes a targets CSV + prints the cost estimate. Nothing faked.

NB: the exact Tracerfy request/response contract must be VERIFIED against the account docs before
the first armed run (see _api_call). Until then the parser FAILS LOUD on any unexpected shape — it
will never write a fabricated phone.
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts import import_resolved

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
# VERIFY against the Tracerfy account docs before the first armed run (endpoint + auth + body).
TRACERFY_API_URL = os.environ.get("TRACERFY_API_URL", "https://api.tracerfy.com/v1/skip-trace")
COST_PER_MATCH = 0.02          # tracerfy.com/pricing: 1 credit/hit = $0.02, misses free, no minimum
BATCH_CAP_USD = 10.0           # over this, an armed run STOPS unless --confirm-spend
DROP_PATTERNS = ["BENTON HARBOR HOPE%", "LAKELAND HOSPITAL%"]
OUT_DIR = "data/incoming"

RESOLVED_COLS = ["lead_id", "owner_name", "phone_1", "phone_1_type", "phone_1_confidence",
                 "phone_1_source", "phone_2", "phone_2_type", "phone_2_confidence",
                 "phone_2_source", "email_1", "verified_via", "resolved_at", "va_notes",
                 "dnc_status", "outreach_recipe", "primary_target"]

TARGET_QUERY = """
    select lead_id, owner_name, owner_mailing_address, owner_mailing_city, owner_mailing_state,
           owner_mailing_zip, situs_address, county_fips, provenance
    from landos.owner_leads
    where tenant_id = %s and not coalesce(discovery_seed, false)
      and owner_type = 'individual'
      and coalesce(skip_trace_status,'not_traced') = 'not_traced'
      and owner_name is not null and btrim(owner_name) <> ''
      and (owner_mailing_address is not null or situs_address is not null)
      and owner_name not ilike all(%s)
      {prov}
    order by lead_id
"""


def split_name(owner_name):
    n = re.sub(r"\s+", " ", (owner_name or "").strip()).replace(",", " ")
    parts = [p for p in n.split() if p]
    if not parts:
        return (None, None)
    if len(parts) == 1:
        return (None, parts[0])
    return (parts[1], parts[0])     # county format LAST FIRST [MIDDLE] -> (first, last)


def addr_parts(t):
    if t.get("owner_mailing_address"):
        return {"street": t["owner_mailing_address"], "city": t.get("owner_mailing_city"),
                "state": t.get("owner_mailing_state"), "zip": t.get("owner_mailing_zip")}
    return {"street": t.get("situs_address"), "city": None, "state": None, "zip": None}


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def select_targets(cur, provenance, limit):
    prov = "and provenance = %s" if provenance else ""
    params = [TENANT, DROP_PATTERNS] + ([provenance] if provenance else [])
    cur.execute(TARGET_QUERY.format(prov=prov), params)
    rows = cur.fetchall()
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- API mode
def _api_call(target, key):
    """One Tracerfy skip-trace request. CONTRACT IS BEST-EFFORT — verify against the account docs
    before arming. Fails loud on an unrecognized response (never fabricates).
    Returns {phones:[{number,type,confidence,dnc}], emails:[...]}"""
    a = addr_parts(target)
    first, last = split_name(target["owner_name"])
    body = {k: v for k, v in {
        "first_name": first, "last_name": last, "name": target["owner_name"],
        "address": a["street"], "city": a["city"], "state": a["state"], "zip": a["zip"],
    }.items() if v}
    req = urllib.request.Request(
        TRACERFY_API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "landos-engine/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Tracerfy HTTP {e.code} (drop-not-fight): {e.read()[:300]!r}")
    # tolerate {result|data|...}; require a recognizable phone-bearing structure or fail loud.
    root = d.get("result") or d.get("data") or d
    persons = root.get("persons") or root.get("matches") or ([root] if isinstance(root, dict) else None)
    if persons is None:
        raise SystemExit(f"Tracerfy response shape unrecognized: keys={list(d)[:8]}. "
                         f"Verify the contract before trusting output — refusing to fabricate.")
    phones, emails, found_phone_field = [], [], False
    for p in persons:
        plist = p.get("phones") or p.get("phoneNumbers") or p.get("phone_numbers")
        if plist is not None:
            found_phone_field = True
        for ph in (plist or []):
            num = ph.get("number") or ph.get("phone") if isinstance(ph, dict) else ph
            if not num:
                continue
            conf = ph.get("confidence") or ph.get("score") if isinstance(ph, dict) else None
            try:
                conf = float(conf)
                conf = conf / 100.0 if conf > 1 else conf
            except (TypeError, ValueError):
                conf = 0.7
            phones.append({"number": num,
                           "type": ((ph.get("type") if isinstance(ph, dict) else None) or "").lower() or None,
                           "confidence": conf,
                           "dnc": bool(isinstance(ph, dict) and (ph.get("dnc") or ph.get("doNotCall") or ph.get("litigator")))})
        for em in (p.get("emails") or []):
            e = em.get("email") if isinstance(em, dict) else em
            if e:
                emails.append(e)
    if not found_phone_field and not emails:
        raise SystemExit("Tracerfy response had no phones/emails field — verify the contract "
                         "(refusing to record a silent miss as a fabricated empty).")
    phones.sort(key=lambda x: -(x["confidence"] or 0))
    return {"phones": phones, "emails": emails}


# ---------------------------------------------------------------- web-export ingest
def _g(row, *names):
    for n in names:
        for k in row:
            if k and k.strip().lower() == n and (row[k] or "").strip():
                return row[k].strip()
    return None


def rows_from_export(path):
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = _g(row, "owner name", "name", "input name", "full name", "owner")
            phones = []
            for i in (1, 2, 3):
                num = _g(row, f"phone {i}", f"phone{i}", f"phone_{i}")
                if num:
                    phones.append({"number": num,
                                   "type": (_g(row, f"phone {i} type", f"phone_{i}_type") or "").lower() or None,
                                   "confidence": 0.7,
                                   "dnc": (_g(row, f"phone {i} dnc", "dnc") or "").lower() in ("true", "yes", "1", "y")})
            if not phones:
                p = _g(row, "phone", "phone number", "mobile")
                if p:
                    phones.append({"number": p, "type": None, "confidence": 0.7, "dnc": False})
            email = _g(row, "email", "email 1", "email_1")
            out.append({"name": name, "phones": phones, "emails": [email] if email else []})
    return out


# ---------------------------------------------------------------- resolved CSV
def write_resolved(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    written = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESOLVED_COLS)
        w.writeheader()
        for t, res in recs:
            ph = res["phones"]
            if not ph and not res["emails"]:
                continue
            row = {c: "" for c in RESOLVED_COLS}
            row["lead_id"] = str(t["lead_id"])
            row["owner_name"] = t["owner_name"]
            for i, p in enumerate(ph[:2], start=1):
                row[f"phone_{i}"] = p["number"]
                row[f"phone_{i}_type"] = p.get("type") or ""
                row[f"phone_{i}_confidence"] = p.get("confidence") or ""
                row[f"phone_{i}_source"] = "tracerfy"
            row["email_1"] = res["emails"][0] if res["emails"] else ""
            row["verified_via"] = "tracerfy"
            row["dnc_status"] = "do_not_call" if (ph and ph[0].get("dnc")) else "unchecked"
            row["va_notes"] = "skip-trace via Tracerfy"
            w.writerow(row)
            written += 1
    return written


# ---------------------------------------------------------------- main
def main(limit=None, provenance=None, from_export=None, confirm_spend=False, dry_run=False):
    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")
            targets = select_targets(cur, provenance, limit)
    by_name = {}
    for t in targets:
        by_name.setdefault(_norm_name(t["owner_name"]), t)
    print(f"qualified individual targets: {len(targets)} (provenance={provenance or 'ALL'}, limit={limit or 'none'})")

    # MODE 1: export ingest
    if from_export:
        if not os.path.exists(from_export):
            sys.exit(f"no such export file: {from_export}")
        recs = []
        for e in rows_from_export(from_export):
            t = by_name.get(_norm_name(e["name"]))
            if t and (e["phones"] or e["emails"]):
                recs.append((t, {"phones": e["phones"], "emails": e["emails"]}))
        out = os.path.join(OUT_DIR, f"tracerfy_resolved_{ts}.csv")
        n = write_resolved(out, recs)
        print(f"export ingest: matched={len(recs)} resolved_written={n} -> {out}")
        if n and not dry_run:
            import_resolved.run(out)
        return

    # MODE 3: no key -> NO-SPEND targets export
    if not key:
        out = os.path.join(OUT_DIR, f"tracerfy_targets_{ts}.csv")
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["lead_id", "owner_name", "mailing_address", "mailing_city",
                        "mailing_state", "mailing_zip", "situs_address", "county_fips", "provenance"])
            for t in targets:
                w.writerow([t["lead_id"], t["owner_name"], t["owner_mailing_address"],
                            t["owner_mailing_city"], t["owner_mailing_state"], t["owner_mailing_zip"],
                            t["situs_address"], t["county_fips"], t["provenance"]])
        print(f"\nNO-SPEND MODE — TRACERFY_API_KEY not set (run blocked on key, by design). "
              f"Wrote {len(targets)} targets -> {out}")
        print(f"  Arm: set TRACERFY_API_KEY then re-run (est ~${len(targets)*COST_PER_MATCH:.2f} "
              f"at ${COST_PER_MATCH}/match, misses free, ${BATCH_CAP_USD:.0f} cap).")
        return

    # MODE 2: armed API
    worst = len(targets) * COST_PER_MATCH
    if worst > BATCH_CAP_USD and not confirm_spend:
        sys.exit(f"STOP — Tracerfy ARMED; up to ${worst:.2f} for {len(targets)} matches, over the "
                 f"${BATCH_CAP_USD:.0f} cap. Re-run with --confirm-spend (or --limit) to proceed.")
    if dry_run:
        print(f"DRY-RUN — would call Tracerfy for {len(targets)} targets (~${worst:.2f}). No calls.")
        return
    recs, hits, errors = [], 0, 0
    for t in targets:
        try:
            res = _api_call(t, key)
        except SystemExit as e:
            print(f"  STOP source on error: {e}")
            errors += 1
            break                       # rate-limit/contract error -> stop the source
        recs.append((t, res))
        if res["phones"]:
            hits += 1
        print(f"  {str(t['lead_id'])[:8]}…  {t['owner_name'][:30]:<30} phones={len(res['phones'])} emails={len(res['emails'])}")
    out = os.path.join(OUT_DIR, f"tracerfy_resolved_{ts}.csv")
    n = write_resolved(out, recs)
    rate = f"{hits}/{len(recs)} ({100*hits/len(recs):.0f}%)" if recs else "0/0"
    print(f"\ntracerfy API: looked_up={len(recs)} hits={hits} hit_rate={rate} resolved_written={n} "
          f"errors={errors} est_spend=${hits*COST_PER_MATCH:.2f} (misses free)")
    if n:
        import_resolved.run(out)
    print("Next: python -m scripts.sync_to_crm (project callable phones; DNC suppressed at the boundary).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tracerfy skip-trace -> import_resolved rail (additive, live vendor)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--provenance", help="scope to one provenance")
    ap.add_argument("--from-export", help="ingest a Tracerfy export CSV instead of calling the API")
    ap.add_argument("--confirm-spend", action="store_true", help="authorize a batch over the cap")
    ap.add_argument("--dry-run", action="store_true", help="select + preview; no API, no writes")
    a = ap.parse_args()
    main(limit=a.limit, provenance=a.provenance, from_export=a.from_export,
         confirm_spend=a.confirm_spend, dry_run=a.dry_run)
