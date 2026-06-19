"""
contact_rank.py — CONTACT-1 B1: Twilio Lookup v2 phone ranker (engine-side).

    python -m scripts.contact_rank [--plan] [--limit N]

Collects EVERY phone held in landos — owner_leads.skip_trace_meta->'phones'
arrays plus landos.contact_relations.phone — dedupes to unique E.164 numbers,
runs each through Twilio Lookup v2 (line_type_intelligence), and writes the
result back where the phone lives:

  owner_leads.skip_trace_meta.phones[i]  +=  line_type, carrier, lookup
      {source:"twilio_lookup_v2", url, fetched_at, confidence}
  owner_leads.skip_trace_meta.phone_rank  =  the lead's numbers ordered
      MOBILES FIRST (mobile > landline > voip > unknown/error), ties broken by
      verified_2x then confidence — the dialer works the list top-down.
  contact_relations.phone_type            =  the looked-up line type.

WRITE SURFACE: landos.owner_leads.skip_trace_meta + landos.contact_relations
ONLY — never public.*, never skip_trace_status / confidence_score (P3's
importer owns those). MERGE, never clobber: only lookup keys are overlaid on
each phone dict; phone_rank is recomputed whole (it is derived data). Idempotent.

HARD GATES (in order):
  1. ENV — TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN must be set. Missing → the
     run STOPS with exit 2 before any network call or DB write. NO mock mode,
     NO fabricated lookup data, ever.
  2. Fictional numbers — the NANP reserved fictional block (XXX-555-0100..0199,
     e.g. worked-example demo rows) is excluded with a logged reason; spending
     real Lookup money on fictional numbers is waste by construction.
  3. Cost is logged: unique lookups × $0.008 (line_type_intelligence list
     price), printed up front; --plan prints the batch + cost and exits
     WITHOUT calling Twilio (a cost preview is not a mock — nothing is written).

Pacing: 3.5s between lookups; on transient failure backoff 25–50s and retry
(2 retries). A persistent failure STOPS the run after committing completed
rows — partials are reported, never papered over.

Provenance: every datum stamped (source, url, fetched_at, confidence). One
manifest JSON in data/ + one append-only landos.owner_leads_run_log row.
"""
import argparse
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.run_manifest import git_commit_info, insert_run_log, utc_now

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
LOOKUP_URL = "https://lookups.twilio.com/v2/PhoneNumbers/{number}?Fields=line_type_intelligence"
COST_PER_LOOKUP = 0.008  # USD, Lookup v2 line_type_intelligence list price
PACE_SECONDS = 3.5       # pierce-pattern pacing
BACKOFF_RANGE = (25, 50) # pierce-pattern failure backoff
MAX_RETRIES = 2
SCRIPT_VERSION = "contact_rank/1.0.0"

# Lookup v2 line_type values → rank bucket (lower = dial first)
TYPE_RANK = {
    "mobile": 0,
    "landline": 1,
    "fixedVoip": 2,
    "nonFixedVoip": 2,
    "voip": 2,
    "personal": 3, "tollFree": 3, "premium": 3, "sharedCost": 3,
    "uan": 3, "voicemail": 3, "pager": 3,
}
UNKNOWN_RANK = 4

# NANP reserved fictional block: NXX-555-0100..0199 (demo/worked-example rows)
FICTIONAL_RE = re.compile(r"^\+1\d{3}55501\d{2}$")


def to_e164(raw: str):
    """US-default E.164 normalization; returns None when not a plausible number."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if (raw or "").strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def twilio_lookup(number: str, sid: str, token: str):
    """One Lookup v2 call. Returns (result_dict, url). Raises on transport error."""
    url = LOOKUP_URL.format(number=urllib.parse.quote(number))
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()), url


def lookup_with_pacing(number: str, sid: str, token: str):
    """Paced lookup with pierce-pattern backoff. Returns outcome dict; a
    Twilio 404 (invalid number) is a real answer, not a transport failure."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            data, url = twilio_lookup(number, sid, token)
            lti = data.get("line_type_intelligence") or {}
            return {
                "ok": True,
                "line_type": lti.get("type"),
                "carrier": lti.get("carrier_name"),
                "lti_error_code": lti.get("error_code"),
                "valid": data.get("valid"),
                "url": url,
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"ok": True, "line_type": None, "carrier": None,
                        "valid": False, "lti_error_code": "HTTP404_invalid_number",
                        "url": LOOKUP_URL.format(number=urllib.parse.quote(number))}
            if e.code in (401, 403):
                raise RuntimeError(f"Twilio auth rejected (HTTP {e.code}) — STOP") from e
            if attempt < MAX_RETRIES:
                wait = random.uniform(*BACKOFF_RANGE)
                print(f"  HTTP {e.code} on {number}; backoff {wait:.0f}s "
                      f"(retry {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < MAX_RETRIES:
                wait = random.uniform(*BACKOFF_RANGE)
                print(f"  transport error on {number} ({e}); backoff {wait:.0f}s "
                      f"(retry {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise


def rank_key(phone: dict):
    """Mobiles first; inside a bucket verified_2x then higher confidence wins."""
    bucket = TYPE_RANK.get(phone.get("line_type") or phone.get("type"), UNKNOWN_RANK)
    return (bucket,
            0 if phone.get("verified_2x") else 1,
            -(phone.get("confidence") or 0))


def collect_phones(cur):
    """All phones in landos: (owner_leads phones arrays, contact_relations rows)."""
    cur.execute(
        """select lead_id, skip_trace_meta from landos.owner_leads
           where tenant_id = %s and skip_trace_meta ? 'phones'""", (TENANT,))
    lead_rows = cur.fetchall()
    cur.execute(
        """select cr.id, cr.phone, cr.phone_type
           from landos.contact_relations cr
           join landos.owner_leads ol on ol.lead_id = cr.owner_lead_id
           where ol.tenant_id = %s and cr.phone is not null""", (TENANT,))
    relation_rows = cur.fetchall()
    return lead_rows, relation_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true",
                    help="print the lookup batch + cost and exit (no Twilio call, no write)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap unique lookups this run (cost control)")
    args = ap.parse_args()

    # ── GATE 1: ENV — STOP before anything else ──────────────────────────────
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not args.plan and (not sid or not token):
        missing = [n for n, v in (("TWILIO_ACCOUNT_SID", sid),
                                  ("TWILIO_AUTH_TOKEN", token)) if not v]
        print("CONTACT-1 B1 STOP — missing ENV: " + ", ".join(missing))
        print("No lookup attempted, nothing written, nothing mocked. "
              "Set the Twilio credentials in .env and re-run.")
        return 2

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        lead_rows, relation_rows = collect_phones(cur)

        # ── inventory + dedupe ───────────────────────────────────────────────
        number_sites = defaultdict(list)   # e164 -> [("lead", lead_id, idx) | ("relation", id)]
        skipped = []                        # (raw, where, reason)
        for lr in lead_rows:
            for i, ph in enumerate(lr["skip_trace_meta"].get("phones") or []):
                e164 = to_e164(ph.get("number"))
                if not e164:
                    skipped.append((ph.get("number"), f"lead {lr['lead_id']}", "unparseable"))
                elif FICTIONAL_RE.match(e164):
                    skipped.append((e164, f"lead {lr['lead_id']}", "reserved fictional block (demo row)"))
                else:
                    number_sites[e164].append(("lead", lr["lead_id"], i))
        for rr in relation_rows:
            e164 = to_e164(rr["phone"])
            if not e164:
                skipped.append((rr["phone"], f"relation {rr['id']}", "unparseable"))
            elif FICTIONAL_RE.match(e164):
                skipped.append((e164, f"relation {rr['id']}", "reserved fictional block (demo row)"))
            else:
                number_sites[e164].append(("relation", rr["id"]))

        numbers = sorted(number_sites)
        if args.limit is not None:
            numbers = numbers[: args.limit]
        cost = len(numbers) * COST_PER_LOOKUP
        print(f"phones found: {sum(len(lr['skip_trace_meta'].get('phones') or []) for lr in lead_rows)} "
              f"on {len(lead_rows)} leads + {len(relation_rows)} relation rows")
        print(f"unique lookable numbers: {len(numbers)}  "
              f"(skipped {len(skipped)}: " +
              ("; ".join(f"{n} [{why}]" for n, _, why in skipped) if skipped else "none") + ")")
        print(f"lookup cost this run: {len(numbers)} x ${COST_PER_LOOKUP} = ${cost:.2f}")

        if args.plan:
            print("PLAN ONLY — no Twilio call made, nothing written.")
            return 0
        if not numbers:
            print("Nothing to rank — no lookable numbers in landos.")
            return 0

        # ── paced lookups ────────────────────────────────────────────────────
        results = {}
        for n, number in enumerate(numbers):
            if n:
                time.sleep(PACE_SECONDS)
            results[number] = lookup_with_pacing(number, sid, token)
            r = results[number]
            print(f"  [{n + 1}/{len(numbers)}] {number} -> "
                  f"{r['line_type'] or 'INVALID'} / {r['carrier'] or '-'}")

        fetched_at = utc_now().isoformat().replace("+00:00", "Z")

        # ── writeback: merge lookup keys into each phone dict, rank per lead ─
        leads_written = relations_written = 0
        for lr in lead_rows:
            meta = lr["skip_trace_meta"]
            phones = meta.get("phones") or []
            touched = False
            for ph in phones:
                e164 = to_e164(ph.get("number"))
                r = results.get(e164)
                if not r:
                    continue
                ph.update({
                    "line_type": r["line_type"],
                    "carrier": r["carrier"],
                    "lookup": {"source": "twilio_lookup_v2", "url": r["url"],
                               "fetched_at": fetched_at,
                               "confidence": 1.0 if r["line_type"] else 0.0,
                               "valid": r["valid"],
                               "error_code": r["lti_error_code"]},
                })
                touched = True
            if not touched:
                continue
            ranked = sorted(phones, key=rank_key)
            merged = {**meta, "phones": phones,
                      "phone_rank": [p.get("number") for p in ranked]}
            cur.execute(
                """update landos.owner_leads set skip_trace_meta=%s, updated_at=now()
                   where tenant_id=%s and lead_id=%s""",
                (Jsonb(merged), TENANT, lr["lead_id"]))
            leads_written += 1
        for rr in relation_rows:
            r = results.get(to_e164(rr["phone"]))
            if r and r["line_type"]:
                cur.execute(
                    "update landos.contact_relations set phone_type=%s where id=%s",
                    (r["line_type"], rr["id"]))
                relations_written += 1

        # ── manifest + run log ───────────────────────────────────────────────
        now = utc_now()
        manifest = {
            "run_id": str(uuid.uuid4()),
            "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
            "kind": "contact_rank",
            "county_fips": "*",
            "git_commit": git_commit_info(os.path.dirname(os.path.dirname(__file__)))
                          .get("git_commit"),
            "scorer_version": SCRIPT_VERSION,
            "row_count": leads_written,
            "lookups": len(numbers),
            "cost_usd": round(cost, 4),
            "skipped": [{"number": n, "where": w, "reason": why} for n, w, why in skipped],
            "line_types": {t: sum(1 for r in results.values() if r["line_type"] == t)
                           for t in {r["line_type"] for r in results.values()}},
            "csv_sha256": None,
        }
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", f"contact_rank_{now.strftime('%Y%m%dT%H%M%SZ')}.manifest.json")
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2)
        insert_run_log(cur, TENANT, {**manifest, "csv_sha256": "n/a"}, path)
        conn.commit()
        print(f"wrote phone_rank on {leads_written} leads, phone_type on "
              f"{relations_written} relations; cost ${cost:.2f}; manifest {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
