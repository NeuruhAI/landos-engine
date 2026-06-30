"""
skip_trace_tracerfy.py — CONTACTABILITY via Tracerfy skip-trace (AXON, additive). THE live vendor.

    python -m scripts.skip_trace_tracerfy                 # DRY-RUN (default): select + plan, $0
    python -m scripts.skip_trace_tracerfy --probe         # trace EXACTLY 1 record (~$0.02), lock shapes
    python -m scripts.skip_trace_tracerfy --batch --confirm   # trace the full pool (gated on --confirm)
    python -m scripts.skip_trace_tracerfy --from-export FILE.csv   # ingest a web export, $0

Vendor is locked to Tracerfy ($0.02/match, NO minimum — misses are free). scripts/skip_trace_batchdata.py
stays dormant. This writes through the EXISTING contact rail (no second writer):

    Tracerfy  ->  resolved CSV (import_resolved schema)  ->  scripts.import_resolved.run()
              ->  landos.owner_leads.skip_trace_meta  ->  sync_to_crm  ->  acquisition_leads

So idempotency, the audit manifest, and the DNC gate are inherited. sync_to_crm suppresses any row whose
skip_trace_meta.dnc_status == 'do_not_call' (sync_to_crm.py: is_dnc check). Phones are stamped
source='tracerfy', verified_via='tracerfy'.

TRACERFY IS ASYNC/BATCH (not a synchronous per-target POST):
    1. POST {BASE}/trace/        body = array of records (or {"records":[...]}) -> { queue_id }
       each record: address_column, city_column, state_column, zip_column,
                     first_name_column, last_name_column, trace_type
    2. GET  {BASE}/queue/:id     poll w/ backoff -> per-record results:
                     primary_phone, mobile_1, mobile_2, landline_1, email_1, primary_phone_type
    3. DNC is SEPARATE: POST {BASE}/dnc/scrub/ -> GET {BASE}/dnc/queue/:id
                     -> national_dnc, litigator, is_clean
BASE defaults to https://tracerfy.com/v1/api ; Bearer auth from TRACERFY_API_KEY (loaded from .env via
app.config). TWO contract ambiguities are env-overridable and locked by --probe:
    TRACERFY_ENVELOPE  = array | wrapped   (raw array body vs {"records":[...]})   default: array
    TRACERFY_TRACE_TYPE                     (the per-record trace_type value)        default: phone

TARGETS = named individuals only: tenant, not discovery_seed, owner_type='individual', a usable name,
a mailing OR situs address, skip_trace_status='not_traced', minus DROP_PATTERNS. Property-keyed rows
(owner_name NULL) are NOT trace-able here — the Tracerfy contract takes a name+address, not an APN, and
those rows carry neither; they need upstream owner/mailing enrichment first (ReportAll/Parcel lane).

FAIL LOUD, NEVER FABRICATE: any API error / timeout / unrecognized shape -> the source STOPS (no partial
writes) or the record is skipped with an explicit reason. A phone is never invented, and a number flagged
national_dnc/litigator is never written as callable (it is dropped from the callable slots; if it is the
only number found the row is kept but stamped dnc_status='do_not_call' so sync suppresses dialing).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts import import_resolved

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
TRACERFY_BASE = os.environ.get("TRACERFY_API_BASE", "https://tracerfy.com/v1/api").rstrip("/")
TRACERFY_ENVELOPE = os.environ.get("TRACERFY_ENVELOPE", "array")   # 'array' | 'wrapped' — probe locks it
TRACERFY_TRACE_TYPE = os.environ.get("TRACERFY_TRACE_TYPE", "phone")  # per-record trace_type — probe locks it
COST_PER_MATCH = 0.02          # tracerfy.com/pricing: 1 credit/hit = $0.02, misses free, no minimum
BATCH_CAP_USD = 10.0           # informational warning threshold (the hard gate is --confirm)
POLL_CAP_SECONDS = 60          # total wall-clock budget per queue poll (backoff 2,4,8,16…)
DEFAULT_CONFIDENCE = 0.7       # Tracerfy's listed poll fields carry NO per-number score; this fixed
                               # default feeds the downstream confidence gate (it is NOT a vendor score).
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
    """A queue never reached a ready state inside the poll budget."""


def _http(method, url, key, payload=None, timeout=40):
    """One JSON request with Bearer auth. Returns parsed JSON (or {} on empty body). Fails loud."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "landos-engine/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise TracerfyHTTPError(e.code, body, url)
    except urllib.error.URLError as e:
        raise TracerfyError(f"network error to {url}: {getattr(e, 'reason', e)}")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise TracerfyContractError(f"non-JSON from {url}: {raw[:200]!r}")


def _wrap(records, envelope):
    """ONE-LINE ENVELOPE SWITCH — raw array vs {"records":[...]}. The probe confirms which the API wants."""
    return records if envelope == "array" else {"records": records}


def _extract_queue_id(d):
    if not isinstance(d, dict):
        return None
    for k in ("queue_id", "queueId", "id", "queue"):
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


def build_record(target):
    """One Tracerfy /trace/ input record (NAME mode — the documented contract)."""
    a = addr_parts(target)
    first, last = split_name(target["owner_name"])
    return {
        "first_name_column": first or "",
        "last_name_column": last or "",
        "address_column": a["street"] or "",
        "city_column": a["city"] or "",
        "state_column": a["state"] or "",
        "zip_column": a["zip"] or "",
        "trace_type": TRACERFY_TRACE_TYPE,
    }


def submit_trace(records, key, envelope=None):
    """POST {BASE}/trace/ -> queue_id. Builds the record array per contract; fails loud if no queue_id."""
    env = envelope or TRACERFY_ENVELOPE
    d = _http("POST", f"{TRACERFY_BASE}/trace/", key, payload=_wrap(records, env))
    qid = _extract_queue_id(d)
    if not qid:
        raise TracerfyContractError(
            f"/trace/ returned no queue_id (envelope={env}); top-level keys={list(d)[:8]}")
    return qid, d


def submit_dnc(numbers, key, envelope=None):
    """POST {BASE}/dnc/scrub/ -> queue_id. Separate compliance endpoint (see contract step 3)."""
    env = envelope or TRACERFY_ENVELOPE
    records = [{"phone": n} for n in numbers]   # phone field name is ambiguous; probe/first-batch confirms
    d = _http("POST", f"{TRACERFY_BASE}/dnc/scrub/", key, payload=_wrap(records, env))
    qid = _extract_queue_id(d)
    if not qid:
        raise TracerfyContractError(
            f"/dnc/scrub/ returned no queue_id (envelope={env}); top-level keys={list(d)[:8]}")
    return qid, d


_PROCESSING = {"processing", "pending", "queued", "running", "in_progress", "working", "started"}
_DONE_OK = {"completed", "complete", "done", "finished", "success", "succeeded", "ready"}
_DONE_BAD = {"failed", "error", "errored", "cancelled", "canceled"}


def _status_of(d):
    if not isinstance(d, dict):
        return None
    for k in ("status", "state", "queue_status"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _results_of(d):
    """Pull the per-record results LIST from a queue payload, defensively. None = not found yet."""
    root = (d.get("result") or d.get("data") or d) if isinstance(d, dict) else d
    if isinstance(root, dict):
        for k in ("results", "records", "matches", "persons", "data", "items", "rows"):
            v = root.get(k)
            if isinstance(v, list):
                return v
        if any(k in root for k in ("primary_phone", "mobile_1", "email_1", "landline_1",
                                   "national_dnc", "is_clean")):
            return [root]                    # a single inline result object
        return None
    if isinstance(root, list):
        return root
    return None


def _poll(url, key, cap=POLL_CAP_SECONDS):
    """Poll a queue URL with exponential backoff (2,4,8,16…) up to `cap` seconds. Fails loud on a
    terminal-error status or on timeout; handles 'still processing' cleanly."""
    waited, delay = 0.0, 2.0
    while True:
        d = _http("GET", url, key)
        st = _status_of(d)
        res = _results_of(d)
        if st in _DONE_BAD:
            raise TracerfyError(f"{url} terminal status={st!r}: {str(d)[:200]}")
        if st in _DONE_OK:
            if res is None:
                raise TracerfyContractError(
                    f"{url} status={st!r} but no results list found; keys={list(d)[:8]}")
            return res, d
        if st not in _PROCESSING and res:    # no/unknown status but results are already present
            return res, d
        if waited >= cap:
            raise TracerfyTimeout(f"{url} not ready after {int(waited)}s (last status={st!r})")
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
    """Map ONE Tracerfy poll record to the engine's {phones:[{number,type,confidence,dnc}], emails:[...]}
    shape. Priority order primary -> mobiles -> landline; only fields the vendor actually returned."""
    phones, seen = [], set()

    def add(num, typ):
        n = (num or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        phones.append({"number": n, "type": typ, "confidence": DEFAULT_CONFIDENCE, "dnc": False})

    add(_val(rec, "primary_phone"), (_val(rec, "primary_phone_type") or "").lower() or None)
    add(_val(rec, "mobile_1", "mobile1"), "mobile")
    add(_val(rec, "mobile_2", "mobile2"), "mobile")
    add(_val(rec, "landline_1", "landline1"), "landline")
    emails = []
    e1 = _val(rec, "email_1", "email1", "email")
    if e1:
        emails.append(e1)
    return {"phones": phones, "emails": emails}


def _name_key_from_target(t):
    first, last = split_name(t["owner_name"])
    return _norm_name((first or "") + (last or ""))


def _name_key_from_result(r):
    if not isinstance(r, dict):
        return ""
    echoed = (r.get("first_name_column") or "") + (r.get("last_name_column") or "")
    return _norm_name(echoed or (r.get("name") or ""))


def _pair_results(results, targets):
    """Map poll results back to targets. Prefer echoed name columns (self-consistent with what we sent);
    else fall back to positional with a STRICT length guard. FAIL LOUD rather than misattribute a phone."""
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
            f"poll returned {len(results)} results for {len(targets)} submitted records and no echoed "
            f"name columns to match on — refusing to misalign phones. Verify the contract via --probe.")
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


def _dnc_index(dres):
    """digits(phone) -> {national_dnc, litigator, is_clean} from the DNC queue results."""
    out = {}
    for r in (dres or []):
        if not isinstance(r, dict):
            continue
        num = _val(r, "phone", "phone_number", "number", "phone_column")
        if not num:
            continue
        out[_digits(num)] = {
            "national_dnc": _as_bool(r.get("national_dnc")),
            "litigator": _as_bool(r.get("litigator")),
            "is_clean": _as_bool(r.get("is_clean")),
        }
    return out


def _apply_dnc(recs, dnc_map):
    """Stamp each phone's dnc flag from the separate scrub. national_dnc OR litigator => flagged."""
    for _, c in recs:
        for p in c["phones"]:
            info = dnc_map.get(_digits(p["number"]))
            if info is not None:
                p["dnc"] = bool(info["national_dnc"] or info["litigator"])


def trace_pool(targets, key, do_dnc=True, envelope=None):
    """Submit ALL targets as ONE async batch, poll, map results, then DNC-scrub the numbers and apply
    flags at the write boundary. Returns (recs, stats). Raises TracerfyError on any failure (the caller
    stops the source — no partial fabrication)."""
    records = [build_record(t) for t in targets]
    qid, _ = submit_trace(records, key, envelope=envelope)
    print(f"  /trace/ accepted {len(records)} records -> queue_id={qid}")
    results, _ = poll_queue(qid, key)
    paired = _pair_results(results, targets)
    recs = [(t, result_to_contacts(r) if r is not None else {"phones": [], "emails": []})
            for t, r in paired]
    scrubbed = 0
    if do_dnc:
        nums = sorted({p["number"] for _, c in recs for p in c["phones"]})
        if nums:
            dqid, _ = submit_dnc(nums, key, envelope=envelope)
            print(f"  /dnc/scrub/ accepted {len(nums)} numbers -> queue_id={dqid}")
            dres, _ = poll_dnc(dqid, key)
            dnc_map = _dnc_index(dres)
            _apply_dnc(recs, dnc_map)
            scrubbed = len(dnc_map)
    return recs, {"submitted": len(records), "results": len(results), "dnc_scrubbed": scrubbed}


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
            name = _g(row, "owner name", "name", "input name", "full name", "owner")
            phones = []
            for i in (1, 2, 3):
                num = _g(row, f"phone {i}", f"phone{i}", f"phone_{i}")
                if num:
                    phones.append({"number": num,
                                   "type": (_g(row, f"phone {i} type", f"phone_{i}_type") or "").lower() or None,
                                   "confidence": DEFAULT_CONFIDENCE,
                                   "dnc": (_g(row, f"phone {i} dnc", "dnc") or "").lower() in ("true", "yes", "1", "y")})
            if not phones:
                p = _g(row, "phone", "phone number", "mobile")
                if p:
                    phones.append({"number": p, "type": None, "confidence": DEFAULT_CONFIDENCE, "dnc": False})
            email = _g(row, "email", "email 1", "email_1")
            out.append({"name": name, "phones": phones, "emails": [email] if email else []})
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


def run_dry(targets, envelope):
    worst = len(targets) * COST_PER_MATCH
    cols = list(build_record(targets[0]).keys()) if targets else [
        "first_name_column", "last_name_column", "address_column", "city_column",
        "state_column", "zip_column", "trace_type"]
    print("DRY-RUN (no network, no spend):")
    print(f"  would trace : {len(targets)} named-individual records")
    print(f"  endpoint    : POST {TRACERFY_BASE}/trace/   envelope={envelope or TRACERFY_ENVELOPE}")
    print(f"  submit cols : {cols}")
    print(f"  trace_type  : {TRACERFY_TRACE_TYPE!r}")
    print(f"  est cost    : {len(targets)} x ${COST_PER_MATCH} = ${worst:.2f} (misses free)")
    if targets:
        print(f"  example rec : lead {str(targets[0]['lead_id'])[:8]}… -> {json.dumps(build_record(targets[0]))}")
    if worst > BATCH_CAP_USD:
        print(f"  NOTE: ${worst:.2f} exceeds the ${BATCH_CAP_USD:.0f} soft cap — --batch still requires --confirm.")
    if not (os.environ.get("TRACERFY_API_KEY") or "").strip():
        print("  NOTE: TRACERFY_API_KEY not loaded — --probe/--batch will refuse until it is set in .env.")


def run_probe(targets, key, envelope):
    if not targets:
        sys.exit("no targets to probe.")
    t0 = targets[0]
    print(f"PROBE — tracing EXACTLY 1 record (lead {str(t0['lead_id'])[:8]}…, ~${COST_PER_MATCH}). "
          f"Locking the /trace/ envelope + response shape. (DNC not exercised here — separate billing.)")
    rec = build_record(t0)
    tries = [envelope] if envelope else []
    tries += [e for e in (TRACERFY_ENVELOPE, "array", "wrapped") if e not in tries]
    qid = used = raw = None
    for e in tries:
        try:
            qid, raw = submit_trace([rec], key, envelope=e)
            used = e
            break
        except TracerfyHTTPError as ex:
            print(f"  envelope={e}: HTTP {ex.code} -> {ex.body[:160]!r}")
            if ex.code in (400, 422):
                continue          # shape rejection -> try the other envelope
            raise
        except TracerfyContractError as ex:
            print(f"  envelope={e}: {ex}")
            continue
    if not qid:
        sys.exit("PROBE FAILED — no envelope accepted by /trace/. Verify base URL / auth / contract.")
    print(f"  ENVELOPE LOCKED: {used!r}   queue_id={qid}")
    print(f"  raw /trace/ response: {json.dumps(raw)[:800]}")
    results, qraw = poll_queue(qid, key)
    print(f"  raw /queue/ response: {json.dumps(qraw)[:1200]}")
    print(f"  parsed {len(results)} result record(s):")
    for r in results[:3]:
        print(f"    raw      : {json.dumps(r)[:400]}")
        print(f"    contacts : {json.dumps(result_to_contacts(r))}")
    print("PROBE COMPLETE — verify result_to_contacts maps the real fields; if so, run --batch --confirm "
          f"(set TRACERFY_ENVELOPE={used} to skip envelope autodetect).")


def run_batch(targets, key, confirm, do_dnc, envelope, ts):
    worst = len(targets) * COST_PER_MATCH
    if not confirm:
        sys.exit(f"STOP — --batch is GATED. Re-run with --confirm to authorize up to ${worst:.2f} for "
                 f"{len(targets)} matches (misses free).")
    if not targets:
        sys.exit("no targets to trace.")
    if worst > BATCH_CAP_USD:
        print(f"WARNING — ${worst:.2f} exceeds the ${BATCH_CAP_USD:.0f} soft cap, proceeding on --confirm.")
    print(f"BATCH — tracing {len(targets)} records (worst case ${worst:.2f}); DNC={'on' if do_dnc else 'OFF'}.")
    try:
        recs, stats = trace_pool(targets, key, do_dnc=do_dnc, envelope=envelope)
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


def main(mode="dry-run", limit=None, provenance=None, from_export=None,
         confirm=False, do_dnc=True, envelope=None):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if from_export:
        run_from_export(from_export, provenance, limit, ts)
        return
    targets = _select(provenance, limit)
    print(f"qualified named-individual targets: {len(targets)} "
          f"(provenance={provenance or 'ALL'}, limit={limit or 'none'})")
    if mode == "dry-run":
        run_dry(targets, envelope)
        return
    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    if not key:
        sys.exit("STOP — TRACERFY_API_KEY not set (loaded from .env via app.config). Cannot probe/batch.")
    if mode == "probe":
        run_probe(targets, key, envelope)
    elif mode == "batch":
        run_batch(targets, key, confirm, do_dnc, envelope, ts)
    else:
        sys.exit(f"unknown mode: {mode}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Tracerfy async skip-trace -> import_resolved rail (additive, live vendor)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_const", dest="mode", const="dry-run",
                   help="select + plan + cost; ZERO network, ZERO spend (default)")
    g.add_argument("--probe", action="store_const", dest="mode", const="probe",
                   help="trace EXACTLY 1 record (~$0.02) to lock the request envelope + response shape")
    g.add_argument("--batch", action="store_const", dest="mode", const="batch",
                   help="trace the full qualified pool (REQUIRES --confirm)")
    ap.set_defaults(mode="dry-run")
    ap.add_argument("--confirm", action="store_true", help="required with --batch to authorize spend")
    ap.add_argument("--confirm-spend", action="store_true", dest="confirm", help=argparse.SUPPRESS)
    ap.add_argument("--limit", type=int, help="scope to the first N targets")
    ap.add_argument("--provenance", help="scope to one provenance")
    ap.add_argument("--from-export", help="ingest a Tracerfy web-export CSV instead of calling the API ($0)")
    ap.add_argument("--no-dnc", action="store_true", help="skip the separate DNC scrub (on by default)")
    ap.add_argument("--envelope", choices=["array", "wrapped"],
                    help="force the /trace/ body envelope (default: array / $TRACERFY_ENVELOPE)")
    a = ap.parse_args()
    main(mode=a.mode, limit=a.limit, provenance=a.provenance, from_export=a.from_export,
         confirm=a.confirm, do_dnc=not a.no_dnc, envelope=a.envelope)
