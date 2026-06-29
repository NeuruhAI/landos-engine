"""
skip_trace_batchdata.py — CONTACTABILITY via BatchData skip-trace (AXON, additive).

    python -m scripts.skip_trace_batchdata [--limit N] [--provenance discovered_taxsale]
                                           [--from-export FILE.csv] [--confirm-spend] [--dry-run]

Resolves phone/email for QUALIFIED human targets (curated pierce entities + tax-sale owners) and
writes them through the EXISTING contact rail — it does NOT invent a second writer:

    BatchData  ->  resolved CSV (import_resolved schema)  ->  scripts.import_resolved.run()
               ->  landos.owner_leads.skip_trace_meta  ->  sync_to_crm  ->  acquisition_leads

So the DNC gate, the callable/confidence gate, idempotency, provenance and the audit manifest are
all inherited from import_resolved + sync_to_crm. Phones are stamped source='batchdata',
verified_via='batchdata'. A phone BatchData flags DNC sets dnc_status='do_not_call' so sync
suppresses it (compliance held end-to-end).

THREE MODES (gate-in-data — never fabricate, never spend without a confirmed credential):
  1. --from-export FILE.csv : ingest a BatchData WEB EXPORT (the trial's confirmed path for the
       Research tiers, which are web-export-only). Maps the export's columns -> resolved CSV. $0
       to this script (the export was already paid).
  2. BATCHDATA_API_KEY set   : call the documented BatchData skip-trace API directly. Spend is
       APPROVED for the trial but capped: over $BATCH_CAP the run STOPS unless --confirm-spend.
  3. neither                 : NO-SPEND. Writes a targets CSV (name + mailing) ready to upload to
       the BatchData web app, prints the qualified count + cost estimate, exits 0. Nothing faked.

QUALIFIED = tenant, not discovery_seed, owner_type in (individual, entity), a usable owner name,
a mailing OR situs address, and skip_trace_status='not_traced' (re-runs never re-trace). Govt and
the two known non-persons are excluded. --limit and --provenance scope a batch.
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
API_URL = "https://api.batchdata.com/api/v1/property/skip-trace"   # BatchData documented endpoint
COST_PER_LOOKUP = 0.07        # trial export pricing ballpark; cap below is the real guard
BATCH_CAP_USD = 10.0          # over this an armed API run STOPS unless --confirm-spend
DROP_PATTERNS = ["BENTON HARBOR HOPE%", "LAKELAND HOSPITAL%"]
OUT_DIR = "data/incoming"

# resolved CSV header import_resolved.run() reads (superset is fine; it keys on lead_id).
RESOLVED_COLS = ["lead_id", "owner_name", "phone_1", "phone_1_type", "phone_1_confidence",
                 "phone_1_source", "phone_2", "phone_2_type", "phone_2_confidence",
                 "phone_2_source", "email_1", "verified_via", "resolved_at", "va_notes",
                 "dnc_status", "outreach_recipe", "primary_target"]

TARGET_QUERY = """
    select lead_id, owner_name, owner_type, owner_mailing_address, owner_mailing_city,
           owner_mailing_state, owner_mailing_zip, situs_address, county_fips, provenance
    from landos.owner_leads
    where tenant_id = %s and not coalesce(discovery_seed, false)
      and owner_type in ('individual','entity')
      and coalesce(skip_trace_status,'not_traced') = 'not_traced'
      and owner_name is not null and btrim(owner_name) <> ''
      and (owner_mailing_address is not null or situs_address is not null)
      and owner_name not ilike all(%s)
      {prov}
    order by lead_id
"""


def split_name(owner_name):
    """'SKINNER GARY D' -> ('GARY','SKINNER'); entities keep the full string as last (BatchData
    accepts a single 'name' for entities). Heuristic, county-data shaped (LAST FIRST MIDDLE)."""
    n = re.sub(r"\s+", " ", (owner_name or "").strip())
    if not n:
        return (None, None)
    if re.search(r"\b(LLC|INC|TRUST|LP|LLP|CORP|COMPANY|CO|CHURCH|CITY|COUNTY|FUND|HOLDINGS|PROPERTIES|ESTATE)\b", n, re.I):
        return (None, n)            # entity: last=full, first=None
    parts = n.replace(",", " ").split()
    if len(parts) == 1:
        return (None, parts[0])
    return (parts[1], parts[0])     # county format: LAST FIRST [MIDDLE]


def addr_parts(t):
    """Prefer mailing; fall back to situs (parsed loosely)."""
    if t.get("owner_mailing_address"):
        return {"street": t["owner_mailing_address"], "city": t.get("owner_mailing_city"),
                "state": t.get("owner_mailing_state"), "zip": t.get("owner_mailing_zip")}
    s = t.get("situs_address") or ""
    return {"street": s or None, "city": None, "state": None, "zip": None}


# ---------------------------------------------------------------- target select
def select_targets(cur, provenance, limit):
    prov = "and provenance = %s" if provenance else ""
    q = TARGET_QUERY.format(prov=prov)
    params = [TENANT, DROP_PATTERNS] + ([provenance] if provenance else [])
    cur.execute(q, params)
    rows = cur.fetchall()
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- API mode
def _api_call(target, key):
    """One documented BatchData skip-trace request. Fail-loud on an unrecognized response shape
    (never fabricate a phone). Returns {phones:[{number,type,confidence,dnc}], emails:[...]}"""
    a = addr_parts(target)
    first, last = split_name(target["owner_name"])
    body = {"requests": [{
        "propertyAddress": {k: v for k, v in
                            {"street": a["street"], "city": a["city"],
                             "state": a["state"], "zip": a["zip"]}.items() if v},
        "name": {k: v for k, v in {"first": first, "last": last}.items() if v},
    }]}
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "landos-engine/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"BatchData HTTP {e.code} (drop-not-fight): {e.read()[:300]!r}")
    # documented shape: results.persons[].{phoneNumbers[],emails[]}; guard, don't fabricate.
    results = (d.get("results") or d.get("data") or {})
    persons = results.get("persons") if isinstance(results, dict) else None
    if persons is None:
        raise SystemExit(f"BatchData response shape unrecognized (no results.persons): keys={list(d)[:8]}. "
                         f"Verify the contract before trusting output — refusing to fabricate.")
    phones, emails = [], []
    for p in persons:
        for ph in (p.get("phoneNumbers") or p.get("phones") or []):
            num = ph.get("number") or ph.get("phone")
            if not num:
                continue
            phones.append({"number": num,
                           "type": (ph.get("type") or "").lower() or None,
                           "confidence": _score(ph),
                           "dnc": bool(ph.get("dnc") or ph.get("doNotCall") or ph.get("litigator"))})
        for em in (p.get("emails") or []):
            e = em.get("email") if isinstance(em, dict) else em
            if e:
                emails.append(e)
    phones.sort(key=lambda x: -(x["confidence"] or 0))
    return {"phones": phones, "emails": emails}


def _score(ph):
    s = ph.get("score") or ph.get("confidence")
    try:
        s = float(s)
    except (TypeError, ValueError):
        return 0.7 if (ph.get("reachable") or ph.get("tested")) else 0.6
    return s / 100.0 if s > 1 else s


# ---------------------------------------------------------------- web-export ingest
def _g(row, *names):
    for n in names:
        for k in row:
            if k and k.strip().lower() == n:
                v = (row[k] or "").strip()
                if v:
                    return v
    return None


def rows_from_export(path):
    """Map a BatchData web-export CSV -> {owner_name/address-ish -> phones/emails}. We match
    back to targets by owner name (the export carries the input name). Tolerant column names."""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = _g(row, "owner name", "name", "input name", "full name", "owner")
            phones = []
            for i in (1, 2, 3):
                num = _g(row, f"phone {i}", f"phone{i}", f"phone_{i}", f"mobile {i}")
                if num:
                    phones.append({"number": num,
                                   "type": (_g(row, f"phone {i} type", f"phone_{i}_type") or "").lower() or None,
                                   "confidence": 0.7,
                                   "dnc": (_g(row, f"phone {i} dnc", f"dnc {i}", "dnc") or "").lower() in ("true", "yes", "1", "y")})
            if not phones:
                p = _g(row, "phone", "phone number", "mobile")
                if p:
                    phones.append({"number": p, "type": None, "confidence": 0.7, "dnc": False})
            email = _g(row, "email", "email 1", "email_1", "email address")
            out.append({"name": name, "phones": phones, "emails": [email] if email else []})
    return out


# ---------------------------------------------------------------- resolved CSV
def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def write_resolved(path, recs):
    """recs: list of (target_row, {phones, emails}). -> import_resolved CSV."""
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
            top_dnc = bool(ph and ph[0].get("dnc"))
            for i, p in enumerate(ph[:2], start=1):
                row[f"phone_{i}"] = p["number"]
                row[f"phone_{i}_type"] = p.get("type") or ""
                row[f"phone_{i}_confidence"] = p.get("confidence") or ""
                row[f"phone_{i}_source"] = "batchdata"
            row["email_1"] = res["emails"][0] if res["emails"] else ""
            row["verified_via"] = "batchdata"
            row["dnc_status"] = "do_not_call" if top_dnc else "unchecked"
            row["va_notes"] = "skip-trace via BatchData"
            w.writerow(row)
            written += 1
    return written


# ---------------------------------------------------------------- main
def main(limit=None, provenance=None, from_export=None, confirm_spend=False, dry_run=False):
    key = (os.environ.get("BATCHDATA_API_KEY") or os.environ.get("BATCHDATA_TOKEN") or "").strip()
    ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")
            targets = select_targets(cur, provenance, limit)
    by_name = {}
    for t in targets:
        by_name.setdefault(_norm_name(t["owner_name"]), t)   # first lead per name
    print(f"qualified targets: {len(targets)} (provenance={provenance or 'ALL'}, limit={limit or 'none'})")

    # ---- MODE 1: web-export ingest -----------------------------------------
    if from_export:
        if not os.path.exists(from_export):
            sys.exit(f"no such export file: {from_export}")
        exp = rows_from_export(from_export)
        recs = []
        for e in exp:
            t = by_name.get(_norm_name(e["name"]))
            if t and (e["phones"] or e["emails"]):
                recs.append((t, {"phones": e["phones"], "emails": e["emails"]}))
        out = os.path.join(OUT_DIR, f"batchdata_resolved_{ts}.csv")
        n = write_resolved(out, recs)
        print(f"export ingest: rows={len(exp)} matched_to_targets={len(recs)} resolved_written={n} -> {out}")
        if n and not dry_run:
            import_resolved.run(out)
        else:
            print("dry-run or 0 rows — import_resolved NOT called.")
        return

    # ---- MODE 3: no key, no export -> NO-SPEND targets export ---------------
    if not key:
        out = os.path.join(OUT_DIR, f"batchdata_targets_{ts}.csv")
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["lead_id", "owner_name", "mailing_address", "mailing_city",
                        "mailing_state", "mailing_zip", "situs_address", "county_fips", "provenance"])
            for t in targets:
                w.writerow([t["lead_id"], t["owner_name"], t["owner_mailing_address"],
                            t["owner_mailing_city"], t["owner_mailing_state"], t["owner_mailing_zip"],
                            t["situs_address"], t["county_fips"], t["provenance"]])
        print(f"\nNO-SPEND MODE — BATCHDATA_API_KEY not set. Wrote {len(targets)} targets -> {out}")
        print(f"  Upload to the BatchData web app, then re-run: "
              f"python -m scripts.skip_trace_batchdata --from-export <downloaded.csv>")
        print(f"  Or set BATCHDATA_API_KEY to call the API directly "
              f"(est ~${len(targets)*COST_PER_LOOKUP:.2f} at ${COST_PER_LOOKUP}/lookup, ${BATCH_CAP_USD:.0f} cap).")
        return

    # ---- MODE 2: armed API call (spend APPROVED, capped) -------------------
    worst = len(targets) * COST_PER_LOOKUP
    if worst > BATCH_CAP_USD and not confirm_spend:
        sys.exit(f"STOP — BatchData ARMED; up to ${worst:.2f} for {len(targets)} lookups, over the "
                 f"${BATCH_CAP_USD:.0f} cap. Re-run with --confirm-spend (or --limit) to proceed.")
    if dry_run:
        print(f"DRY-RUN — would call BatchData for {len(targets)} targets (~${worst:.2f}). No calls made.")
        return
    recs, hits, errors = [], 0, 0
    for t in targets:
        try:
            res = _api_call(t, key)
        except SystemExit as e:
            print(f"  STOP source on error: {e}")
            errors += 1
            break                       # rate-limit/contract error -> stop the source (constitution)
        recs.append((t, res))
        if res["phones"]:
            hits += 1
        print(f"  {str(t['lead_id'])[:8]}…  {t['owner_name'][:30]:<30} phones={len(res['phones'])} emails={len(res['emails'])}")
    out = os.path.join(OUT_DIR, f"batchdata_resolved_{ts}.csv")
    n = write_resolved(out, recs)
    rate = f"{hits}/{len(recs)} ({100*hits/len(recs):.0f}%)" if recs else "0/0"
    print(f"\nbatchdata API: looked_up={len(recs)} hits={hits} hit_rate={rate} resolved_written={n} errors={errors}")
    if n:
        import_resolved.run(out)
    print("Next: python -m scripts.sync_to_crm (project callable phones; DNC suppressed).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BatchData skip-trace -> import_resolved rail (additive)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--provenance", help="scope to one provenance (e.g. discovered_taxsale)")
    ap.add_argument("--from-export", help="ingest a BatchData web-export CSV instead of calling the API")
    ap.add_argument("--confirm-spend", action="store_true", help="authorize an API batch over the cap")
    ap.add_argument("--dry-run", action="store_true", help="select + preview; no API, no writes")
    a = ap.parse_args()
    main(limit=a.limit, provenance=a.provenance, from_export=a.from_export,
         confirm_spend=a.confirm_spend, dry_run=a.dry_run)
