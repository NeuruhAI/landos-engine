"""
test_run_manifest.py — provenance/versioning layer (additive, no scoring change).

Covers: sha256 of the output file matches the file, the manifest JSON is written
with the required fields, git info degrades honestly when not a repo, and the
append-only run_log insert produces the right row.
"""
import hashlib
import json

from datetime import datetime, timezone

from app.run_manifest import (
    sha256_file, git_commit_info, manifest_filename, build_manifest,
    write_manifest, insert_run_log,
)
from app.score_absentee import weights_snapshot, __VERSION__

DT = datetime(2026, 6, 4, 13, 34, 18, tzinfo=timezone.utc)


def _manifest(csv_path, sha):
    return build_manifest(
        run_id="11111111-1111-1111-1111-111111111111",
        generated_at=DT, county_fips="26021", county_alias="berrien",
        git_info=git_commit_info("/"), scorer_version=__VERSION__,
        weights_snapshot=weights_snapshot(), row_count=2, input_apn_count=2,
        reportall_calls=None, csv_path=str(csv_path), csv_sha256=sha,
        top_5=[{"rank": 1, "owner_name": "X", "apn": "a", "final_lead_score": 100.0}],
    )


def test_sha256_matches_file(tmp_path):
    p = tmp_path / "out.csv"
    p.write_bytes(b"rank,owner\n1,ACME LLC\n")
    assert sha256_file(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_manifest_written_and_hash_matches(tmp_path):
    csv = tmp_path / "owner_leads_berrien.csv"
    csv.write_bytes(b"rank,owner\n1,ACME LLC\n2,JOHN SMITH\n")
    sha = sha256_file(str(csv))
    manifest = _manifest(csv, sha)
    path = write_manifest(str(tmp_path), "berrien", manifest, DT)
    # file exists, is valid JSON, and the recorded hash matches the actual file bytes
    loaded = json.loads(open(path).read())
    assert loaded["csv_sha256"] == sha == hashlib.sha256(csv.read_bytes()).hexdigest()
    for key in ("run_id", "generated_at_utc", "county_fips", "git_commit",
                "scorer_version", "weights_snapshot", "row_count", "csv_sha256", "top_5"):
        assert key in loaded
    assert loaded["generated_at_utc"] == "2026-06-04T13:34:18Z"


def test_manifest_filename_format():
    assert manifest_filename("berrien", DT) == "owner_leads_berrien_20260604T133418Z.manifest.json"


def test_git_info_not_a_repo_is_dirty(tmp_path):
    # a fresh tmp dir is not a git repo -> 'dirty', never a fabricated commit
    info = git_commit_info(str(tmp_path))
    assert info["git_commit"] == "dirty"
    assert info["changed_files"] == ["<not a git repository>"]


def test_weights_snapshot_records_active_constants():
    snap = weights_snapshot()
    assert snap["scorer_version"] == __VERSION__
    assert snap["overlap_cap"] == 1.60
    assert snap["tier_base"]["out_of_state"] == 90.0


class _FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params


def test_run_log_insert_row(tmp_path):
    csv = tmp_path / "owner_leads_berrien.csv"
    csv.write_bytes(b"rank,owner\n1,ACME LLC\n")
    sha = sha256_file(str(csv))
    manifest = _manifest(csv, sha)
    cur = _FakeCursor()
    insert_run_log(cur, "tenant-1", manifest, "/data/owner_leads_berrien_x.manifest.json")
    assert "insert into landos.owner_leads_run_log" in cur.sql
    # params order: tenant, run_id, generated_at, county_fips, git_commit,
    #               scorer_version, row_count, csv_sha256, manifest_path
    assert cur.params[0] == "tenant-1"
    assert cur.params[1] == manifest["run_id"]
    assert cur.params[3] == "26021"
    assert cur.params[6] == 2              # row_count
    assert cur.params[7] == sha            # csv_sha256
    assert cur.params[8].endswith(".manifest.json")
