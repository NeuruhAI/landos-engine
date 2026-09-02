"""T6: the NCSBE voter-file lane (scripts/run_nc_free_phone_enrichment.py) over all seven
CURRENT counties, run as a subprocess into a fresh custody run dir; its
CANDIDATE_CONTACT_RESOLUTION.csv and phone_append_combined.csv become the receipt
and the per-row attempts. The spine is written later by project_contacts_to_deedsonar."""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from typing import Dict, List, Optional

from .attempts import Attempt
from .receipts import RouteReceipt

SOURCE = "NCSBE_PUBLIC_VOTER_FILE"
PROVENANCE_URL = "https://s3.amazonaws.com/dl.ncsbe.gov/data/"
TERMS = "Public bulk files, free download, no access control; birthdate / SSN / driver's license excluded by state law."


def run_lane(out_dir: str, *, ncsbe_dir: str, estate_dir: Optional[str] = None, full_rematch: bool = False, extra_args: Optional[List[str]] = None, python: str = sys.executable) -> subprocess.CompletedProcess:
    cmd = [python, "-m", "scripts.run_nc_free_phone_enrichment", "--out-dir", out_dir, "--ncsbe-dir", ncsbe_dir, "--skip-entities"]
    if estate_dir:
        cmd += ["--estate-dir", estate_dir]
    if full_rematch:
        cmd.append("--full-rematch")
    cmd += extra_args or []
    return subprocess.run(cmd, capture_output=True, text=True)


def receipt_from_run(out_dir: str, estate_rows: List[dict], *, run_id: str, now_iso: str, receipt_path: Optional[str], source_as_of: Optional[str]) -> Dict:
    """Read the lane's outputs; HIT = the row has an NCSBE-sourced phone or identity in the run."""
    phones_by_cand: Dict[str, int] = {}
    comb = os.path.join(out_dir, "phone_append_combined.csv")
    if os.path.exists(comb):
        with open(comb, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if SOURCE in (r.get("phone_source") or r.get("source") or "").upper() or "NCSBE" in (r.get("phone_source") or r.get("source") or "").upper():
                    phones_by_cand[r.get("candidate_id", "")] = phones_by_cand.get(r.get("candidate_id", ""), 0) + 1
    resolution: Dict[str, dict] = {}
    res = os.path.join(out_dir, "CANDIDATE_CONTACT_RESOLUTION.csv")
    if os.path.exists(res):
        with open(res, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                resolution[r.get("candidate_id", "")] = r
    attempts: List[Attempt] = []; hits = 0
    for r in estate_rows:
        cid = r["candidate_id"]; n = phones_by_cand.get(cid, 0); res_row = resolution.get(cid, {})
        identity = (res_row.get("contact_resolution_state") or "").upper()
        hit = n > 0 or identity in ("CONTACT_FOUND", "CONTACT_FOUND_HIGH", "IDENTITY_FOUND")
        if hit:
            hits += 1
        attempts.append(Attempt(cid, "T6", run_id, "HIT" if hit else "NO_HIT", SOURCE, source_as_of, now_iso, receipt_path,
                                {"phones": n, "resolution": identity or None}))
    receipt = RouteReceipt("T6", "NCSBE voter registration (all seven CURRENT counties)", "AUTOMATED", "NC State Board of Elections county voter files", TERMS, PROVENANCE_URL,
                           rows_tried=len(estate_rows), hits=hits, new_contacts=sum(phones_by_cand.values()), new_heirs=0, source_as_of=source_as_of, observed_at=now_iso,
                           counties=sorted({r.get("county", "") for r in estate_rows}), evidence={"run_dir": out_dir, "resolution_rows": len(resolution), "phone_rows": sum(phones_by_cand.values())})
    return {"receipt": receipt, "attempts": attempts}
