"""
hunt_worker.py — HUNT-V2 §3: the hunt-job runner (census -> stage -> score ->
DRY-RUN mint -> awaiting_operator_mint).

    python -m app.hunt_worker [--job <id>]

Night-chain-compatible: one pass over every queued job (or the one named),
per-step receipts, exit 0 when every processed job parks at
awaiting_operator_mint or uncovered, exit 1 if any job failed.

Stages (each appends a CHAINED receipt to hunt_jobs.receipts):
  censusing — evidence-only $0 coverage census per county
              (hunt_core.census_county). Zero covered counties in the whole
              territory -> status 'uncovered' with the per-county reasons.
  staging   — covered counties with a staged JSONL load into
              landos.discovery_signals via scripts/load_taxsale_county.load_file
              VERBATIM (load_signals mapping, idempotent on
              (source_type, source_ref), freshness-gated upcoming-only).
  scoring   — scripts.score_emergence in DRY-RUN. Doctrine collision resolved
              in the doctrine's favor: score_emergence's real run WRITES
              owner_leads.emerging_score, and the directive's bold rule — no
              owner_leads write before the operator fires — WINS. Net-new
              signals cannot score anyway (only matched+dated temporal signals
              emerge; matching happens at mint). The REAL score_emergence runs
              inside app.hunt_mint, after the human-fired mint.
  dry-run mint — scripts.mint_taxsale_leads.main(dry_run=True, only_fips=...)
              -> would_mint / already_present / rejected_stale per county into
              counts.would_mint, then status 'awaiting_operator_mint'.

**No write to landos.owner_leads happens here.** The mint is HUMAN-FIRED:
    python -m app.hunt_mint <job_id>     (prints the dry-run table, typed GO)
"""
import argparse
import sys

import psycopg
from psycopg.rows import dict_row

from scripts import mint_taxsale_leads, score_emergence
from scripts.load_taxsale_county import load_file

from .config import DATABASE_URL
from . import hunt_core


def _persist(cur, conn, job, **kw):
    hunt_core.update_job(cur, job["id"], receipts=job["receipts"], **kw)
    conn.commit()


def run_job(conn, cur, job):
    """Advance ONE queued job to awaiting_operator_mint / uncovered / failed.
    Returns the terminal-for-this-runner status."""
    counts = dict(job.get("counts") or {})
    signal_types = (job.get("recipe") or {}).get("signal_types") or ["taxsale"]
    territory = job["territory"]
    print(f"\n=== hunt job {job['id']} — {len(territory)} county(ies), "
          f"signals={signal_types} ===")

    # ---- censusing ---------------------------------------------------------
    hunt_core.update_job(cur, job["id"], status="censusing")
    conn.commit()
    census = [hunt_core.census_county(cur, e, signal_types) for e in territory]
    counts["census"] = {c["fips"]: c for c in census}
    hunt_core.append_receipt(job, "census", {"census": census})
    covered = [c for c in census if c["covered"]]
    for c in census:
        tag = "COVERED" if c["covered"] else "UNCOVERED"
        print(f"  [{c['fips']} {c['county']}, {c['state']}] {tag} "
              f"staged_rows={c['staged_rows']} db_signals={c['db_signals']}")
    if not covered:
        _persist(cur, conn, job, status="uncovered", counts=counts,
                 error="; ".join(c["reason"] for c in census))
        print("  -> UNCOVERED (no $0 evidence anywhere in the territory; reasons recorded)")
        return "uncovered"

    # ---- staging (idempotent, freshness-gated, discovery_signals ONLY) ------
    hunt_core.update_job(cur, job["id"], status="staging", counts=counts)
    conn.commit()
    staged = {}
    for c in covered:
        if c["staged_file"]:
            read, ins, skip, rej = load_file(cur, c["staged_file"], dry_run=False)
            staged[c["fips"]] = {"read": read, "inserted": ins,
                                 "skipped_dup": skip, "rejected_stale": rej}
            print(f"  staged [{c['fips']}]: read={read} inserted={ins} "
                  f"dup={skip} stale_rejected={rej}")
        else:
            staged[c["fips"]] = {"read": 0, "inserted": 0, "skipped_dup": 0,
                                 "rejected_stale": 0,
                                 "note": "covered by existing discovery_signals rows"}
    counts["staged"] = staged
    hunt_core.append_receipt(job, "stage", {"staged": staged})
    _persist(cur, conn, job, status="scoring", counts=counts)   # commits staging too

    # ---- scoring (DRY-RUN — the real run belongs to the human-fired mint) ---
    score_emergence.main(dry_run=True)
    fips_list = [c["fips"] for c in covered]
    cur.execute(
        """select county_fips, count(*) as signals,
                  count(*) filter (where matched_lead_id is not null) as matched
           from landos.discovery_signals
           where county_fips = any(%s) and source_type = any(%s)
           group by county_fips""", (fips_list, list(signal_types)))
    scoring = {r["county_fips"]: {"signals": r["signals"], "matched": r["matched"]}
               for r in cur.fetchall()}
    counts["scoring"] = {
        "per_county": scoring,
        "note": ("net-new signals emerge post-mint (matched+dated doctrine); "
                 "real score_emergence runs in app.hunt_mint")}
    hunt_core.append_receipt(job, "score", counts["scoring"])
    _persist(cur, conn, job, counts=counts)

    # ---- DRY-RUN mint -> the operator's table -------------------------------
    stats = mint_taxsale_leads.main(dry_run=True, only_fips=fips_list)
    counts["would_mint"] = {
        "total": stats["would_mint"], "already_present": stats["already_present"],
        "rejected_stale": stats["rejected_stale"], "per_county": stats["per_county"]}
    hunt_core.append_receipt(job, "dry_run_mint",
                             {k: counts["would_mint"][k]
                              for k in ("total", "already_present",
                                        "rejected_stale", "per_county")})
    _persist(cur, conn, job, status="awaiting_operator_mint", counts=counts)
    print(f"  -> AWAITING_OPERATOR_MINT (would_mint={stats['would_mint']}, "
          f"already_present={stats['already_present']}). "
          f"Fire with: python -m app.hunt_mint {job['id']}")
    return "awaiting_operator_mint"


def main(job_id=None):
    failed = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        if job_id:
            job = hunt_core.get_job(cur, job_id)
            if job is None:
                raise SystemExit(f"no hunt job {job_id}")
            if job["status"] != "queued":
                raise SystemExit(f"job {job_id} is {job['status']!r}, not 'queued' — "
                                 f"the worker only advances queued jobs")
            jobs = [job]
        else:
            cur.execute("select id from landos.hunt_jobs where status = 'queued' "
                        "order by created_at")
            jobs = [hunt_core.get_job(cur, r["id"]) for r in cur.fetchall()]
        if not jobs:
            print("hunt_worker: no queued jobs.")
            return 0
        for job in jobs:
            try:
                run_job(conn, cur, job)
            except Exception as e:  # noqa: BLE001 — a job failure must not kill the chain
                conn.rollback()
                failed += 1
                hunt_core.append_receipt(job, "failed", {"error": str(e)})
                hunt_core.update_job(cur, job["id"], status="failed", error=str(e),
                                     receipts=job["receipts"])
                conn.commit()
                print(f"  job {job['id']} FAILED: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HUNT-V2 job runner (proposes; never mints)")
    ap.add_argument("--job", help="advance one queued job id (default: all queued)")
    sys.exit(main(job_id=ap.parse_args().job))
