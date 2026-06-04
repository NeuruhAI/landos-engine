"""
run_manifest.py — tamper-evident run manifests for lead-list output.

Every time scripts/owner_leads.py produces a ranked CSV, it also writes a manifest
JSON next to it and appends one append-only row to landos.owner_leads_run_log. The
manifest hashes the CSV bytes, timestamps the run, and ties the list to the exact
code/config version (git HEAD + scorer_version + the fusion weights in effect) so
any lead list is reproducible and auditable. This is provenance only — it changes
nothing about scoring.

Pure/standalone helpers (no DB) + one DB insert helper, kept here so they can be
unit-tested without running the whole script.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def sha256_file(path: str) -> str:
    """SHA-256 of a file's bytes (hashed AFTER it is written)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit_info(repo_dir: str) -> Dict[str, Any]:
    """Current git HEAD short hash, or 'dirty' + changed files. If the directory
    is not a git repo, records 'dirty' with an explanatory changed-files entry —
    never fabricates a commit."""
    def _git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(
                ["git", "-C", repo_dir, *args],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip()

    head = _git("rev-parse", "--short", "HEAD")
    if head is None:
        return {"git_commit": "dirty", "changed_files": ["<not a git repository>"]}
    porcelain = _git("status", "--porcelain")
    if porcelain:  # working tree has uncommitted changes
        changed = [line[3:] for line in porcelain.splitlines() if line.strip()]
        return {"git_commit": "dirty", "based_on_commit": head, "changed_files": changed}
    return {"git_commit": head, "changed_files": []}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def manifest_filename(county_alias: str, dt: datetime) -> str:
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    return f"owner_leads_{county_alias.lower()}_{stamp}.manifest.json"


def build_manifest(
    *,
    run_id: str,
    generated_at: datetime,
    county_fips: str,
    county_alias: str,
    git_info: Dict[str, Any],
    scorer_version: str,
    weights_snapshot: Dict[str, Any],
    row_count: int,
    input_apn_count: int,
    reportall_calls: Optional[int],
    csv_path: str,
    csv_sha256: str,
    top_5: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
        "county_fips": county_fips,
        "county_alias": county_alias,
        "git_commit": git_info.get("git_commit"),
        "git_changed_files": git_info.get("changed_files", []),
        "git_based_on_commit": git_info.get("based_on_commit"),
        "scorer_version": scorer_version,
        "weights_snapshot": weights_snapshot,
        "row_count": row_count,
        "input_apn_count": input_apn_count,
        "reportall_calls": reportall_calls,
        "csv_file": os.path.basename(csv_path),
        "csv_sha256": csv_sha256,
        "top_5": top_5,
    }


def write_manifest(out_dir: str, county_alias: str, manifest: Dict[str, Any],
                   dt: datetime) -> str:
    """Write the manifest JSON next to the CSV; return its full path."""
    path = os.path.join(out_dir, manifest_filename(county_alias, dt))
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
    return path


def insert_run_log(cur, tenant_id: str, manifest: Dict[str, Any], manifest_path: str) -> None:
    """Append one permanent audit row. Append-only — never updates/overwrites."""
    cur.execute(
        """
        insert into landos.owner_leads_run_log
          (tenant_id, run_id, generated_at_utc, county_fips, git_commit,
           scorer_version, row_count, csv_sha256, manifest_path)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (tenant_id, manifest["run_id"], manifest["generated_at_utc"],
         manifest["county_fips"], manifest["git_commit"], manifest["scorer_version"],
         manifest["row_count"], manifest["csv_sha256"], manifest_path),
    )
