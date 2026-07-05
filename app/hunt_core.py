"""
hunt_core.py — HUNT-V2 shared core: job records, per-stage receipt chain,
$0 coverage census. Used by the API routes (app/main.py), the runner
(app/hunt_worker.py), the operator-fire CLI (app/hunt_mint.py) and the demo
CLI (app/hunt.py).

AUTONOMY DOCTRINE: everything in this module PROPOSES. Nothing here writes
landos.owner_leads — the mint is fired by a human (app.hunt_mint, typed GO).

$0 CENSUS DISCIPLINE (never guessed): a county is COVERED only by evidence —
staged signal JSONL on disk (the landos-ingest lane) or discovery_signals rows
already in the spine. A known free FEED with nothing staged is reported (so the
operator knows the ingest lane to run) but the county stays UNCOVERED until
real rows exist. The engine never fetches raw sources itself — repo firewall:
fetching/staging is landos-ingest's job.
"""
import glob
import json
import os
import re
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .receipt import section_hash, sha256_text

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
INGEST_ROOT = os.path.expanduser("~/repos/landos-ingest/data/strategy/signals")
STAGED_TAXSALE_DIR = os.path.join(INGEST_ROOT, "tax_sale", "staged")

# ---- $0 source registry (DATA — the census consults, never guesses) ----------
# state -> known free feeds and HOW their rows arrive. Informational for the
# uncovered reason; coverage itself requires staged/DB evidence.
KNOWN_FREE_FEEDS = {
    "MI": [{"feed": "tax-sale.info", "signal_type": "taxsale",
            "how": "landos-ingest MI statewide firehose stages <county>_taxsale_signals.jsonl"}],
    "TX": [{"feed": "county tax-sale postings (MVBA-pattern PDF lanes)", "signal_type": "taxsale",
            "how": "county-specific staging (Williamson WILCO pattern) into discovery_signals"}],
    "IN": [{"feed": "county sheriff/tax-sale postings", "signal_type": "taxsale",
            "how": "landos-ingest staged lanes (stjoseph pattern)"}],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def staged_key(county_name):
    """Census county name -> landos-ingest staged-file key:
    'St. Clair County' -> 'saint_clair', 'Presque Isle County' -> 'presque_isle'."""
    s = (county_name or "").lower()
    s = re.sub(r"\s+county$", "", s.strip())
    s = s.replace("st.", "saint").replace("ste.", "sainte")
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", "_", s.strip())


# ---- receipt chain (the /lead-receipt idiom, per job stage) -------------------
def stage_receipt(prev_chain_hash, stage, body):
    """One chained stage receipt: sha256 over the stage's canonical body,
    chain-hashed on the previous stage's chain hash (GENESIS for the first)."""
    body_hash = section_hash(body)
    chain = sha256_text((prev_chain_hash or "GENESIS") + body_hash)
    return {"stage": stage, "at": now_iso(), "body": body,
            "body_hash": body_hash, "prev": prev_chain_hash, "chain_hash": chain}


def append_receipt(job, stage, body):
    """Append a chained receipt to the job dict IN MEMORY (persist via update_job)."""
    receipts = list(job.get("receipts") or [])
    prev = receipts[-1]["chain_hash"] if receipts else None
    receipts.append(stage_receipt(prev, stage, body))
    job["receipts"] = receipts
    return receipts[-1]


# ---- job records ---------------------------------------------------------------
def create_job(cur, territory, recipe, requested_by=None, tenant_id=TENANT):
    """INSERT a queued hunt job. territory must already be census-resolved
    ([{fips, state, county}]) — resolution failures belong to the caller (422)."""
    recipe = dict(recipe or {})
    recipe.setdefault("signal_types", ["taxsale"])
    recipe.setdefault("thresholds", {})
    cur.execute(
        """insert into landos.hunt_jobs (tenant_id, requested_by, territory, recipe)
           values (%s, %s, %s, %s)
           returning id, tenant_id, requested_by, territory, recipe, status,
                     counts, receipts, error, created_at, updated_at""",
        (tenant_id, requested_by, Jsonb(territory), Jsonb(recipe)))
    return cur.fetchone()


def get_job(cur, job_id):
    cur.execute(
        """select id, tenant_id, requested_by, territory, recipe, status,
                  counts, receipts, error, created_at, updated_at
           from landos.hunt_jobs where id = %s""", (job_id,))
    return cur.fetchone()


def update_job(cur, job_id, *, status=None, counts=None, receipts=None, error=None):
    """Persist job progress. Only the fields passed move; updated_at always does."""
    sets, params = ["updated_at = now()"], []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if counts is not None:
        sets.append("counts = %s")
        params.append(Jsonb(counts))
    if receipts is not None:
        sets.append("receipts = %s")
        params.append(Jsonb(receipts))
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    params.append(job_id)
    cur.execute(f"update landos.hunt_jobs set {', '.join(sets)} where id = %s", params)


# ---- coverage census ($0, evidence-only) ---------------------------------------
def census_county(cur, entry, signal_types):
    """Evidence census for one resolved county entry. Returns
    {covered: bool, staged_file, staged_rows, db_signals, db_new_signals,
     known_feeds, reason?}. COVERED requires real rows (disk or DB) — a known
    feed alone is reported but does NOT cover (never guessed)."""
    fips, state, county = entry["fips"], entry["state"], entry["county"]
    out = {"fips": fips, "state": state, "county": county,
           "staged_file": None, "staged_rows": 0,
           "db_signals": 0, "db_new_signals": 0,
           "known_feeds": [f for f in KNOWN_FREE_FEEDS.get(state, [])
                           if f["signal_type"] in signal_types]}

    if "taxsale" in signal_types:
        path = os.path.join(STAGED_TAXSALE_DIR, f"{staged_key(county)}_taxsale_signals.jsonl")
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    out["staged_rows"] = sum(1 for line in fh if line.strip())
                out["staged_file"] = path
            except OSError:
                pass  # unreadable file is NOT evidence

    cur.execute(
        """select count(*) as n, count(*) filter (where status = 'new') as new
           from landos.discovery_signals
           where county_fips = %s and source_type = any(%s)""",
        (fips, list(signal_types)))
    row = cur.fetchone()
    out["db_signals"], out["db_new_signals"] = row["n"], row["new"]

    out["covered"] = bool(out["staged_rows"] or out["db_signals"])
    if not out["covered"]:
        feeds = ", ".join(f["feed"] for f in out["known_feeds"]) or "none known"
        out["reason"] = (f"no $0 evidence for {county}, {state} ({fips}): no staged "
                         f"signal file, 0 discovery_signals rows. Known free feeds: "
                         f"{feeds} — stage via landos-ingest, then re-hunt.")
    return out
