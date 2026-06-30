"""
skip_trace_tracerfy.py — CONTACTABILITY via Tracerfy skip-trace (AXON, additive). THE live vendor.

    python -m scripts.skip_trace_tracerfy                 # DRY-RUN (default): select + plan, $0
    python -m scripts.skip_trace_tracerfy --probe         # trace EXACTLY 1 record (~$0.02), lock shapes
    python -m scripts.skip_trace_tracerfy --batch --confirm        # trace the full pool (gated on --confirm)
    python -m scripts.skip_trace_tracerfy --batch --confirm --dnc  # + DNC scrub (separate ~$0.02/PHONE)
    python -m scripts.skip_trace_tracerfy --from-export FILE.csv    # ingest a web export, $0

Vendor is locked to Tracerfy. This writes through the EXISTING contact rail (no second writer):

    Tracerfy  ->  resolved CSV (import_resolved schema)  ->  scripts.import_resolved.run()
              ->  landos.owner_leads.skip_trace_meta  ->  sync_to_crm  ->  acquisition_leads

So idempotency, the audit manifest, and the DNC gate are inherited. sync_to_crm suppresses any row whose
skip_trace_meta.dnc_status == 'do_not_call'. Phones are stamped source/verified_via='tracerfy'.

REAL TRACERFY CONTRACT (verified against tracerfy.com/skip-tracing-api-documentation, 2026-06):
    1. POST {BASE}/trace/   multipart/form-data:
         csv_file = <a CSV of the people to trace, header row required>
         + form fields that MAP your CSV's column headers to Tracerfy's fields:
           address_column, city_column, state_column, zip_column,
           first_name_column, last_name_column,
           mail_address_column, mail_city_column, mail_state_column, mailing_zip_column,
           trace_type  ('normal'=1 credit/$0.02 | 'advanced'=2 credits | 'custom')
       -> { "queue_id": <int>, "status":"pending", ... }
    2. GET  {BASE}/queue/:id   poll; ready when "pending": false, then results come from "download_url"
       (a CSV) — or inline. Per-record: primary_phone, primary_phone_type, mobile_1..5, landline_1..3,
       email_1..5 (and echoed first_name/last_name/address for matching).
    3. DNC is SEPARATE and BILLS PER PHONE (1 credit each): POST {BASE}/dnc/scrub/ accepts JSON
       {"phones":[...]} -> { "dnc_queue_id": <int> } ; GET {BASE}/dnc/queue/:id -> CSV columns
       phone,label,national_dnc,state_dnc,dma,litigator,phone_type,is_clean.
BASE defaults to https://tracerfy.com/v1/api ; Bearer auth from TRACERFY_API_KEY (loaded from .env via
app.config). TRACERFY_TRACE_TYPE (default 'normal') is env-overridable.

TARGETS = named individuals only: tenant, not discovery_seed, owner_type='individual', a usable name,
a mailing OR situs address, skip_trace_status='not_traced', minus DROP_PATTERNS. Property-keyed rows
(owner_name NULL) are NOT trace-able here — Tracerfy takes name+address, not an APN, and those rows
carry neither; they need upstream owner/mailing enrichment first (ReportAll/Parcel lane).

FAIL LOUD, NEVER FABRICATE: any API error / timeout / unrecognized shape -> the source STOPS (no partial
writes). A phone is never invented. DNC write boundary: a number that is not is_clean (national_dnc /
state_dnc / dma / litigator) is never written callable — it is dropped from the callable slots; if it is
the only number found the row is kept but stamped dnc_status='do_not_call' so sync suppresses dialing.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts import import_resolved

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
TRACERFY_BASE = os.environ.get("TRACERFY_API_BASE", "https://tracerfy.com/v1/api").rstrip("/")
TRACERFY_TRACE_TYPE = os.environ.get("TRACERFY_TRACE_TYPE", "normal")   # normal | advanced | custom
COST_PER_CREDIT = 0.02         # tracerfy.com/pricing: 1 credit = $0.02; misses free, no minimum
CREDITS_PER_TRACE = {"normal": 1, "advanced": 2, "custom": 1}
BATCH_CAP_USD = 10.0           # informational warning threshold (the hard gate is --confirm)
POLL_CAP_SECONDS = 180         # total wall-clock budget per queue poll (backoff 2,4,8,16…)
DEFAULT_CONFIDENCE = 0.7       # Tracerfy's result fields carry NO per-number score; this fixed default
                               # feeds the downstream confidence gate (it is NOT a vendor score).
DROP_PATTERNS = ["BENTON HARBOR HOPE%", "LAKELAND HOSPITAL%"]
OUT_DIR = "data/incoming"

RESOLVED_COLS = ["lead_id", "owner_name", "phone_1", "phone_1_type", "phone_1_confidence",
                 "phone_1_source", "phone_2", "phone_2_type", "phone_2_confidence",
                 "phone_2_source", "email_1", "verified_via", "resolved_at", "va_notes",
                 "dnc_status", "outreach_recipe", "primary_target"]

# Headers we write into the uploaded csv_file, and the form fields that map them for Tracerfy.
CSV_HEADERS = ["first_name", "last_name", "address", "city", "state", "zip",
               "mail_address", "mail_city", "mail_state", "mailing_zip"]
TRACE_COLUMN_MAP = {
    "first_name_column": "first_name", "last_name_column": "last_name",
    "address_column": "address", "city_column": "city", "state_column": "state", "zip_column": "zip",
    "mail_address_column": "mail_address", "mail_city_column": "mail_city",
    "mail_state_column": "mail_state", "mailing_zip_column": "mailing_zip",
}

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


# ---------------------------------------------------------------- target selection (unchanged)
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


# ================================================================ Tracerfy async API layer
class TracerfyError(RuntimeError):
    """Base — any Tracerfy failure. Caller stops the source (never fabricates a partial result)."""


class TracerfyHTTPError(TracerfyError):
    def __init__(self, code, body, url):
        self.code, self.body, self.url = code, body, url
        super().__init__(f"HTTP {code} from {url}: {body[:200]!r}")


class TracerfyContractError(TracerfyError):
    """The response did not match the documented shape — refuse to trust/fabricate it."""


class TracerfyTimeout(TracerfyError):
    """A queue never reached pending=false inside the poll budget."""


def _request(method, url, key, *, body=None, content_type=None, timeout=60):
    """One Bearer-authed request; returns the raw response bytes. Fails loud on HTTP/network error."""
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "landos-engine/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        b = ""
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise TracerfyHTTPError(e.code, b, url)
    except urllib.error.URLError as e:
        raise TracerfyError(f"network error to {url}: {getattr(e, 'reason', e)}")


def _json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise TracerfyContractError(f"expected JSON, got: {raw[:200]!r}")


def _parse_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def _multipart(fields, file_field, filename, file_bytes):
    """Build a multipart/form-data body (stdlib only). Returns (content_type, body_bytes)."""
    boundary = "----landosFormBoundary" + uuid.uuid4().hex
    nl = "\r\n"
    buf = io.BytesIO()

    def w(s):
        buf.write(s.encode("utf-8") if isinstance(s, str) else s)

    for name, value in fields.items():
        w(f"--{boundary}{nl}")
        w(f'Content-Disposition: form-data; name="{name}"{nl}{nl}')
        w(f"{value}{nl}")
    w(f"--{boundary}{nl}")
    w(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"{nl}')
    w(f"Content-Type: text/csv{nl}{nl}")
    w(file_bytes)
    w(nl)
    w(f"--{boundary}--{nl}")
    return f"multipart/form-data; boundary={boundary}", buf.getvalue()


def _extract_queue_id(d):
    if not isinstance(d, dict):
        return None
    for k in ("queue_id", "dnc_queue_id", "queueId", "id", "queue"):
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    for k in ("result", "data"):
        sub = d.get(k)
        if isinstance(sub, dict):
            q = _extract_queue_id(sub)
            if q:
                return q
    return None


def build_trace_row(t):
    """One CSV row for the upload. address/* = the best COMPLETE address we hold (mailing-preferred via
    addr_parts); mail_* mirror it (we don't split situs into city/state, so we never fabricate them)."""
    a = addr_parts(t)
    first, last = split_name(t["owner_name"])
    return {
        "first_name": first or "", "last_name": last or "",
        "address": a["street"] or "", "city": a["city"] or "",
        "state": a["state"] or "", "zip": a["zip"] or "",
        "mail_address": (t.get("owner_mailing_address") or a["street"] or ""),
        "mail_city": (t.get("owner_mailing_city") or a["city"] or ""),
        "mail_state": (t.get("owner_mailing_state") or a["state"] or ""),
        "mailing_zip": (t.get("owner_mailing_zip") or a["zip"] or ""),
    }


def build_trace_csv(targets):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_HEADERS)
    w.writeheader()
    for t in targets:
        w.writerow(build_trace_row(t))
    return buf.getvalue().encode("utf-8")


def submit_trace(targets, key):
    """POST {BASE}/trace/ as multipart (csv_file + column map) -> queue_id. Fails loud on no queue_id."""
    fields = dict(TRACE_COLUMN_MAP)
    fields["trace_type"] = TRACERFY_TRACE_TYPE
    ct, body = _multipart(fields, "csv_file", "targets.csv", build_trace_csv(targets))
    d = _json(_request("POST", f"{TRACERFY_BASE}/trace/", key, body=body, content_type=ct))
    qid = _extract_queue_id(d)
    if not qid:
        raise TracerfyContractError(f"/trace/ returned no queue_id; top-level keys={list(d)[:8]}")
    return qid, d


def submit_dnc(numbers, key):
    """POST {BASE}/dnc/scrub/ as JSON {"phones":[...]} -> dnc_queue_id. SEPARATE per-phone billing."""
    body = json.dumps({"phones": list(numbers)}).encode("utf-8")
    d = _json(_request("POST", f"{TRACERFY_BASE}/dnc/scrub/", key, body=body,
                       content_type="application/json"))
    qid = _extract_queue_id(d)
    if not qid:
        raise TracerfyContractError(f"/dnc/scrub/ returned no queue id; top-level keys={list(d)[:8]}")
    return qid, d


def _poll(url, key, cap=POLL_CAP_SECONDS):
    """Poll a queue with backoff (2,4,8,16…) up to `cap`s. Ready when the body is a non-empty list OR an
    object with pending=false; results come inline or from download_url (a CSV). Fails loud on timeout."""
    waited, delay = 0.0, 2.0
    while True:
        d = _json(_request("GET", url, key))
        if isinstance(d, list):
            if d:
                return d
        else:
            pending = d.get("pending")
            done = (pending is False) or (pending is None and (d.get("download_url") or d.get("results")))
            if done:
                for k in ("results", "records", "data", "matches"):
                    if isinstance(d.get(k), list):
                        return d[k]
                dl = d.get("download_url")
                if dl:
                    return _parse_csv(_request("GET", dl, key).decode("utf-8", "replace"))
                raise TracerfyContractError(
                    f"{url} reports complete but no results list / download_url; keys={list(d)[:8]}")
        if waited >= cap:
            raise TracerfyTimeout(f"{url} still pending after {int(waited)}s")
        nap = min(delay, cap - waited)
        time.sleep(nap)
        waited += nap
        delay = min(delay * 2, 16.0)


def poll_queue(queue_id, key, cap=POLL_CAP_SECONDS):
    return _poll(f"{TRACERFY_BASE}/queue/{queue_id}", key, cap)


def poll_dnc(queue_id, key, cap=POLL_CAP_SECONDS):
    return _poll(f"{TRACERFY_BASE}/dnc/queue/{queue_id}", key, cap)


def _val(rec, *keys):
    if not isinstance(rec, dict):
        return None
    for k in keys:
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def result_to_contacts(rec):
    """Map ONE Tracerfy result row to {phones:[{number,type,confidence,dnc}], emails:[...]}. Priority
    primary -> mobiles -> landlines; only fields the vendor actually returned (no fabrication)."""
    phones, seen = [], set()

    def add(num, typ):
        n = (num or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        phones.append({"number": n, "type": typ, "confidence": DEFAULT_CONFIDENCE, "dnc": False})

    add(_val(rec, "primary_phone"), (_val(rec, "primary_phone_type") or "").lower() or None)
    for i in range(1, 6):
        add(_val(rec, f"mobile_{i}"), "mobile")
    for i in range(1, 4):
        add(_val(rec, f"landline_{i}"), "landline")
    emails = []
    for i in range(1, 6):
        e = _val(rec, f"email_{i}")
        if e:
            emails.append(e)
    return {"phones": phones, "emails": emails}


def _name_key_from_target(t):
    first, last = split_name(t["owner_name"])
    return _norm_name((first or "") + (last or ""))


def _name_key_from_result(r):
    if not isinstance(r, dict):
        return ""
    return _norm_name((r.get("first_name") or "") + (r.get("last_name") or ""))


def _pair_results(results, targets):
    """Map results back to targets. Prefer the echoed first_name/last_name; else positional with a STRICT
    length guard. FAIL LOUD rather than misattribute a phone to the wrong person."""
    if results and all(_name_key_from_result(r) for r in results):
        idx = {}
        for r in results:
            idx.setdefault(_name_key_from_result(r), []).append(r)
        out = []
        for t in targets:
            bucket = idx.get(_name_key_from_target(t)) or []
            out.append((t, bucket.pop(0) if bucket else None))
        return out
    if len(results) != len(targets):
        raise TracerfyContractError(
            f"queue returned {len(results)} results for {len(targets)} submitted records and no echoed "
            f"names to match on — refusing to misalign phones. Verify via --probe.")
    return list(zip(targets, results))


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y", "t")
    return False


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _dnc_index(rows):
    """digits(phone) -> flag dict from the DNC result CSV rows."""
    out = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        num = _val(r, "phone", "phone_number", "number")
        if not num:
            continue
        out[_digits(num)] = {
            "national_dnc": _as_bool(r.get("national_dnc")),
            "state_dnc": _as_bool(r.get("state_dnc")),
            "dma": _as_bool(r.get("dma")),
            "litigator": _as_bool(r.get("litigator")),
            "is_clean": _as_bool(r.get("is_clean")),
        }
    return out


def _apply_dnc(recs, dnc_map):
    """Stamp dnc on each phone. Strict: callable ONLY if is_clean (i.e. national/state/dma/litigator
    all false). A number Tracerfy did not return in the scrub is left unflagged (unchecked)."""
    for _, c in recs:
        for p in c["phones"]:
            info = dnc_map.get(_digits(p["number"]))
            if info is None:
                continue
            p["dnc"] = not info["is_clean"] or info["national_dnc"] or info["state_dnc"] \
                or info["dma"] or info["litigator"]


def trace_pool(targets, key, do_dnc=False):
    """Submit ALL targets as ONE multipart batch, poll, map, then (optionally) DNC-scrub the numbers and
    apply flags at the write boundary. Returns (recs, stats). Raises TracerfyError on any failure."""
    qid, _ = submit_trace(targets, key)
    print(f"  /trace/ accepted {len(targets)} rows -> queue_id={qid}")
    results = poll_queue(qid, key)
    recs = [(t, result_to_contacts(r) if r is not None else {"phones": [], "emails": []})
            for t, r in _pair_results(results, targets)]
    scrubbed = 0
    if do_dnc:
        nums = sorted({p["number"] for _, c in recs for p in c["phones"]})
        if nums:
            dqid, _ = submit_dnc(nums, key)
            print(f"  /dnc/scrub/ accepted {len(nums)} numbers -> dnc_queue_id={dqid}")
            _apply_dnc(recs, _dnc_index(poll_dnc(dqid, key)))
            scrubbed = len(nums)
    return recs, {"submitted": len(targets), "results": len(results), "dnc_scrubbed": scrubbed}


# ---------------------------------------------------------------- web-export ingest ($0)
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
            first = _g(row, "first_name", "first name")
            last = _g(row, "last_name", "last name")
            name = (f"{last} {first}".strip() if (first or last)
                    else _g(row, "owner name", "name", "input name", "full name", "owner"))
            phones = []
            primary = _g(row, "primary_phone", "primary phone")
            if primary:
                phones.append({"number": primary,
                               "type": (_g(row, "primary_phone_type", "primary phone type") or "").lower() or None,
                               "confidence": DEFAULT_CONFIDENCE, "dnc": False})
            for i in range(1, 6):
                m = _g(row, f"mobile_{i}", f"mobile {i}")
                if m:
                    phones.append({"number": m, "type": "mobile", "confidence": DEFAULT_CONFIDENCE, "dnc": False})
            for i in range(1, 4):
                ln = _g(row, f"landline_{i}", f"landline {i}")
                if ln:
                    phones.append({"number": ln, "type": "landline", "confidence": DEFAULT_CONFIDENCE, "dnc": False})
            if not phones:
                p = _g(row, "phone", "phone number", "mobile")
                if p:
                    phones.append({"number": p, "type": None, "confidence": DEFAULT_CONFIDENCE, "dnc": False})
            emails = [e for e in (_g(row, f"email_{i}", f"email {i}") for i in range(1, 6)) if e]
            if not emails:
                e = _g(row, "email")
                if e:
                    emails = [e]
            out.append({"name": name, "phones": phones, "emails": emails})
    return out


# ---------------------------------------------------------------- resolved CSV (write boundary)
def write_resolved(path, recs):
    """Write the import_resolved-schema CSV. DNC WRITE BOUNDARY: a flagged number is never written as
    callable. Prefer clean numbers; if the only numbers are flagged, keep the row (lead not dropped) but
    stamp dnc_status='do_not_call' so sync_to_crm suppresses dialing."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    written = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESOLVED_COLS)
        w.writeheader()
        for t, res in recs:
            phones, emails = res["phones"], res["emails"]
            clean = [p for p in phones if not p.get("dnc")]
            flagged = [p for p in phones if p.get("dnc")]
            if clean:
                use, dnc_status = clean, "unchecked"
            elif flagged:
                use, dnc_status = flagged[:1], "do_not_call"     # only DNC numbers -> keep row, suppress
            else:
                use, dnc_status = [], "unchecked"
            if not use and not emails:
                continue
            row = {c: "" for c in RESOLVED_COLS}
            row["lead_id"] = str(t["lead_id"])
            row["owner_name"] = t["owner_name"]
            for i, p in enumerate(use[:2], start=1):
                row[f"phone_{i}"] = p["number"]
                row[f"phone_{i}_type"] = p.get("type") or ""
                row[f"phone_{i}_confidence"] = p.get("confidence") or ""
                row[f"phone_{i}_source"] = "tracerfy"
            row["email_1"] = emails[0] if emails else ""
            row["verified_via"] = "tracerfy"
            row["dnc_status"] = dnc_status
            row["va_notes"] = "skip-trace via Tracerfy (async)"
            w.writerow(row)
            written += 1
    return written


# ---------------------------------------------------------------- modes
def _est(n):
    credits = CREDITS_PER_TRACE.get(TRACERFY_TRACE_TYPE, 1)
    return n * credits * COST_PER_CREDIT, credits


def _select(provenance, limit):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")
            return select_targets(cur, provenance, limit)


def run_from_export(from_export, provenance, limit, ts):
    if not os.path.exists(from_export):
        sys.exit(f"no such export file: {from_export}")
    targets = _select(provenance, limit)
    by_name = {}
    for t in targets:
        by_name.setdefault(_norm_name(t["owner_name"]), t)
    recs = []
    for e in rows_from_export(from_export):
        t = by_name.get(_norm_name(e["name"]))
        if t and (e["phones"] or e["emails"]):
            recs.append((t, {"phones": e["phones"], "emails": e["emails"]}))
    out = os.path.join(OUT_DIR, f"tracerfy_resolved_{ts}.csv")
    n = write_resolved(out, recs)
    print(f"export ingest ($0): matched={len(recs)} resolved_written={n} -> {out}")
    if n:
        import_resolved.run(out)


def run_dry(targets):
    worst, credits = _est(len(targets))
    print("DRY-RUN (no network, no spend):")
    print(f"  would trace : {len(targets)} named-individual records")
    print(f"  transport   : multipart/form-data, file field 'csv_file' -> POST {TRACERFY_BASE}/trace/")
    print(f"  csv headers : {CSV_HEADERS}")
    print(f"  column map  : {json.dumps(TRACE_COLUMN_MAP)}")
    print(f"  trace_type  : {TRACERFY_TRACE_TYPE!r} ({credits} credit/lead)")
    print(f"  est cost    : {len(targets)} x {credits} x ${COST_PER_CREDIT} = ${worst:.2f} (misses free)")
    print(f"  DNC         : OFF by default — separate ~${COST_PER_CREDIT}/PHONE charge (enable with --dnc)")
    if targets:
        print(f"  example row : {json.dumps(build_trace_row(targets[0]))}")
    if worst > BATCH_CAP_USD:
        print(f"  NOTE: ${worst:.2f} exceeds the ${BATCH_CAP_USD:.0f} soft cap — --batch still needs --confirm.")
    if not (os.environ.get("TRACERFY_API_KEY") or "").strip():
        print("  NOTE: TRACERFY_API_KEY not loaded — --probe/--batch will refuse until it is set in .env.")


def run_probe(targets, key):
    if not targets:
        sys.exit("no targets to probe.")
    t0 = targets[0]
    print(f"PROBE — tracing EXACTLY 1 record (lead {str(t0['lead_id'])[:8]}…, ~${COST_PER_CREDIT}, "
          f"trace_type={TRACERFY_TRACE_TYPE!r}). DNC not exercised (separate per-phone billing).")
    try:
        qid, raw = submit_trace([t0], key)
    except TracerfyError as ex:
        sys.exit(f"PROBE /trace/ failed: {ex}")
    print(f"  queue_id={qid}   raw POST response: {json.dumps(raw)[:600]}")
    try:
        results = poll_queue(qid, key)
    except TracerfyError as ex:
        sys.exit(f"PROBE /queue/ failed: {ex}")
    print(f"  /queue/ returned {len(results)} record(s):")
    for r in results[:3]:
        print(f"    raw      : {json.dumps(r)[:500]}")
        print(f"    contacts : {json.dumps(result_to_contacts(r))}")
    print("PROBE COMPLETE — confirm the result fields above map cleanly; then run --batch --confirm "
          "(add --dnc only when you want the separate per-phone DNC spend).")


def run_batch(targets, key, confirm, do_dnc, ts):
    worst, credits = _est(len(targets))
    if not confirm:
        sys.exit(f"STOP — --batch is GATED. Re-run with --confirm to authorize up to ${worst:.2f} for "
                 f"{len(targets)} matches at {credits} credit/lead (misses free).")
    if not targets:
        sys.exit("no targets to trace.")
    if worst > BATCH_CAP_USD:
        print(f"WARNING — ${worst:.2f} exceeds the ${BATCH_CAP_USD:.0f} soft cap, proceeding on --confirm.")
    dnc_note = f"ON (+~${COST_PER_CREDIT}/phone, separate spend)" if do_dnc else "OFF"
    print(f"BATCH — tracing {len(targets)} records (worst case ${worst:.2f}); DNC={dnc_note}.")
    try:
        recs, stats = trace_pool(targets, key, do_dnc=do_dnc)
    except TracerfyError as ex:
        sys.exit(f"BATCH STOPPED (no partial fabrication): {ex}")
    out = os.path.join(OUT_DIR, f"tracerfy_resolved_{ts}.csv")
    n = write_resolved(out, recs)
    with_contact = sum(1 for _, c in recs if c["phones"] or c["emails"])
    print(f"\ntracerfy BATCH: submitted={stats['submitted']} results={stats['results']} "
          f"with_contact={with_contact} resolved_written={n} dnc_scrubbed={stats['dnc_scrubbed']} -> {out}")
    if n:
        import_resolved.run(out)
    print("Next: python -m scripts.sync_to_crm (projects callable phones; DNC suppressed at the boundary).")


def main(mode="dry-run", limit=None, provenance=None, from_export=None, confirm=False, do_dnc=False):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if from_export:
        run_from_export(from_export, provenance, limit, ts)
        return
    targets = _select(provenance, limit)
    print(f"qualified named-individual targets: {len(targets)} "
          f"(provenance={provenance or 'ALL'}, limit={limit or 'none'})")
    if mode == "dry-run":
        run_dry(targets)
        return
    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    if not key:
        sys.exit("STOP — TRACERFY_API_KEY not set (loaded from .env via app.config). Cannot probe/batch.")
    if mode == "probe":
        run_probe(targets, key)
    elif mode == "batch":
        run_batch(targets, key, confirm, do_dnc, ts)
    else:
        sys.exit(f"unknown mode: {mode}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Tracerfy async skip-trace -> import_resolved rail (additive, live vendor)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_const", dest="mode", const="dry-run",
                   help="select + plan + cost; ZERO network, ZERO spend (default)")
    g.add_argument("--probe", action="store_const", dest="mode", const="probe",
                   help="trace EXACTLY 1 record (~$0.02) to lock the request + response shapes")
    g.add_argument("--batch", action="store_const", dest="mode", const="batch",
                   help="trace the full qualified pool (REQUIRES --confirm)")
    ap.set_defaults(mode="dry-run")
    ap.add_argument("--confirm", action="store_true", help="required with --batch to authorize spend")
    ap.add_argument("--confirm-spend", action="store_true", dest="confirm", help=argparse.SUPPRESS)
    ap.add_argument("--dnc", action="store_true",
                    help="also run the DNC scrub (SEPARATE ~$0.02/phone spend; off by default)")
    ap.add_argument("--limit", type=int, help="scope to the first N targets")
    ap.add_argument("--provenance", help="scope to one provenance")
    ap.add_argument("--from-export", help="ingest a Tracerfy web-export CSV instead of calling the API ($0)")
    a = ap.parse_args()
    main(mode=a.mode, limit=a.limit, provenance=a.provenance, from_export=a.from_export,
         confirm=a.confirm, do_dnc=a.dnc)
