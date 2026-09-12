"""The DeedSonar contact graph has ONE database authority, and the legacy spine is refused.

Three layers:
  1. the resolver (app/contact_authority.py): source order and every refusal;
  2. the inventory: every engine file that opens a database AND touches the contact
     graph resolves through the resolver. A new contact writer that reads
     DATABASE_URL directly fails here;
  3. the real CLIs: each contact tool, pointed at the legacy DSN, exits 2 with
     LEGACY_CONTACT_DSN_REFUSED before it connects or writes an output file.

No test connects to any database. The legacy DSNs below point at 127.0.0.1:1, so a
guard regression would surface as a connection error, never as a network write.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.contact_authority import (
    CANONICAL,
    CANONICAL_REF,
    NON_PRODUCTION,
    NONCANONICAL_ALLOWED,
    ContactAuthorityError,
    resolve_contact_dsn,
)

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "lvdimujkfheeyqchjzra"

POOLER_LEGACY = f"postgresql://postgres.{LEGACY}:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
DIRECT_LEGACY = f"postgresql://postgres:pw@db.{LEGACY}.supabase.co:5432/postgres"
CONNINFO_LEGACY = f"host=aws-0-us-east-1.pooler.supabase.com user=postgres.{LEGACY} password=pw dbname=postgres"
POOLER_CANON = f"postgresql://postgres.{CANONICAL_REF}:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
DIRECT_CANON = f"postgresql://postgres:pw@db.{CANONICAL_REF}.supabase.co:5432/postgres"
OTHER_REF = "abcdefghijklmnopqrst"


def resolve(**env):
    return resolve_contact_dsn(env={k: v for k, v in env.items() if v is not None})


# --- 1. the resolver -------------------------------------------------------------

@pytest.mark.parametrize("dsn", [POOLER_LEGACY, DIRECT_LEGACY, CONNINFO_LEGACY])
@pytest.mark.parametrize("var", ["DEEDSONAR_DATABASE_URL", "DATABASE_URL"])
def test_legacy_is_refused_in_every_form_and_source(dsn, var):
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(**{var: dsn})
    assert exc.value.code == "LEGACY_CONTACT_DSN_REFUSED"
    assert "pw" not in str(exc.value)


def test_legacy_is_refused_even_with_the_noncanonical_override():
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(DEEDSONAR_DATABASE_URL=POOLER_LEGACY, CONTACT_DB_ALLOW_NONCANONICAL="1")
    assert exc.value.code == "LEGACY_CONTACT_DSN_REFUSED"


def test_legacy_named_only_by_pg_env_is_refused():
    # A DSN with no host/user takes them from PGHOST / PGUSER; libpq would reach legacy.
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(DEEDSONAR_DATABASE_URL="dbname=postgres", PGUSER=f"postgres.{LEGACY}",
                PGHOST="aws-0-us-east-1.pooler.supabase.com")
    assert exc.value.code == "LEGACY_CONTACT_DSN_REFUSED"


def test_explicit_database_url_argument_is_checked_too():
    with pytest.raises(ContactAuthorityError) as exc:
        resolve_contact_dsn(POOLER_LEGACY, env={"DEEDSONAR_DATABASE_URL": POOLER_CANON})
    assert exc.value.code == "LEGACY_CONTACT_DSN_REFUSED"


@pytest.mark.parametrize("dsn", [POOLER_CANON, DIRECT_CANON])
def test_canonical_efxo_is_accepted(dsn):
    auth = resolve(DEEDSONAR_DATABASE_URL=dsn)
    assert (auth.klass, auth.ref, auth.source) == (CANONICAL, CANONICAL_REF, "DEEDSONAR_DATABASE_URL")
    assert "pw" not in auth.describe() and CANONICAL_REF in auth.describe()


def test_deedsonar_var_wins_over_the_engine_database_url():
    # The host's real shape after T5's .env line: the engine keeps legacy, contacts go to efxo.
    auth = resolve(DEEDSONAR_DATABASE_URL=POOLER_CANON, DATABASE_URL=POOLER_LEGACY)
    assert auth.ref == CANONICAL_REF and auth.source == "DEEDSONAR_DATABASE_URL"


def test_the_host_today_fails_closed():
    # .env today: DATABASE_URL=legacy and no DEEDSONAR_DATABASE_URL -> refusal, not a legacy write.
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(DATABASE_URL=POOLER_LEGACY)
    assert exc.value.code == "LEGACY_CONTACT_DSN_REFUSED"
    assert "DEEDSONAR_DATABASE_URL" in str(exc.value)


@pytest.mark.parametrize("dsn", [
    f"postgresql://postgres.{OTHER_REF}:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
    f"postgresql://postgres:pw@db.{OTHER_REF}.supabase.co:5432/postgres",
    "postgresql://postgres:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",  # ref unreadable
])
def test_any_other_supabase_project_is_refused(dsn):
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(DEEDSONAR_DATABASE_URL=dsn, CONTACT_DB_ALLOW_NONCANONICAL="1")
    assert exc.value.code == "NONCANONICAL_SUPABASE_REFUSED"


@pytest.mark.parametrize("dsn", [
    "postgresql://t:t@127.0.0.1:55432/replica",
    "postgresql://t@localhost/replica",
    "postgresql:///replica?host=/tmp",
    "dbname=replica",
])
def test_local_replicas_and_test_clusters_are_non_production(dsn):
    assert resolve(DEEDSONAR_DATABASE_URL=dsn).klass == NON_PRODUCTION


def test_an_unprovable_remote_host_needs_the_explicit_override():
    dsn = "postgresql://postgres:pw@10.0.0.7:5432/postgres"
    with pytest.raises(ContactAuthorityError) as exc:
        resolve(DEEDSONAR_DATABASE_URL=dsn)
    assert exc.value.code == "UNPROVEN_CONTACT_HOST_REFUSED"
    assert resolve(DEEDSONAR_DATABASE_URL=dsn, CONTACT_DB_ALLOW_NONCANONICAL="1").klass == NONCANONICAL_ALLOWED


def test_missing_dsn_is_an_error_unless_optional():
    with pytest.raises(ContactAuthorityError) as exc:
        resolve()
    assert exc.value.code == "CONTACT_DSN_MISSING"
    assert resolve_contact_dsn(required=False, env={}) is None
    assert resolve_contact_dsn("", required=False, env={"DATABASE_URL": POOLER_LEGACY}) is None


def test_the_env_example_placeholder_is_not_a_dsn():
    auth = resolve(DEEDSONAR_DATABASE_URL=f"postgresql://postgres.{CANONICAL_REF}:[YOUR-PASSWORD]@h/p",
                   DATABASE_URL=POOLER_CANON)
    assert auth.source == "DATABASE_URL"


# --- 2. the inventory ------------------------------------------------------------

CONTACT_GRAPH = re.compile(
    r"\bpublic\.(candidate_contact_resolution|parties|party_phones|candidate_parties|contact_evidence"
    r"|free_route_attempts|entity_identity|candidate_entity_identity|tier_c_adjudication"
    r"|case_source_attempt|address_semantics_repair_log|deedsonar_current_candidates_v1)\b")
OPENS_A_DB = re.compile(r"psycopg\.connect\(|DATABASE_URL|database_url")
DIRECT_ENV_READ = re.compile(
    r"from app\.config import[^\n]*DATABASE_URL|environ(\.get)?\(?\[?[\"']DATABASE_URL|getenv\([\"']DATABASE_URL")

#: every engine tool that connects to the DeedSonar contact graph (T3 contact-DB repair, 2026-09-11)
GUARDED = {
    "scripts/project_contacts_to_deedsonar.py",   # contact projection (party graph + ccr)
    "scripts/refresh_next_best_source.py",        # NBE refresh (ccr.next_source)
    "scripts/import_operator_estate_research.py", # operator importer (human research)
    "scripts/run_free_routes.py",                 # free routes incl. NCSBE T6 -> free_route_attempts
    "scripts/record_estate_index_search.py",
    "scripts/resolve_estate_household_family.py",
    "scripts/project_household_identity.py",
    "scripts/project_entity_identities.py",
    "scripts/revalidate_identity_delta.py",
    "scripts/adjudicate_tier_c.py",
    "scripts/build_operator_manual_queue.py",
    "scripts/repair_address_semantics.py",
}
#: libraries that receive an already-resolved DSN from a GUARDED caller
RESOLVED_BY_CALLER = {
    "app/free_routes/attempts.py": "scripts/run_free_routes.py",
}


def _engine_py():
    for base in ("scripts", "app"):
        for p in sorted((ROOT / base).rglob("*.py")):
            yield p.relative_to(ROOT).as_posix(), p.read_text(errors="replace")


def test_every_contact_graph_connection_goes_through_the_authority():
    touching = {rel for rel, s in _engine_py() if CONTACT_GRAPH.search(s) and OPENS_A_DB.search(s)}
    assert touching == GUARDED | set(RESOLVED_BY_CALLER), (
        "a file that connects to the contact graph is missing from, or gone from, the inventory: "
        f"new={sorted(touching - GUARDED - set(RESOLVED_BY_CALLER))} "
        f"gone={sorted(GUARDED | set(RESOLVED_BY_CALLER) - touching)}")
    for rel in GUARDED:
        s = (ROOT / rel).read_text()
        assert "contact_dsn_or_exit" in s, rel
        assert not DIRECT_ENV_READ.search(s), f"{rel} reads DATABASE_URL around the authority"
    for lib, caller in RESOLVED_BY_CALLER.items():
        assert not DIRECT_ENV_READ.search((ROOT / lib).read_text()), lib
        assert "contact_dsn_or_exit" in (ROOT / caller).read_text(), caller


def test_the_legacy_ref_lives_only_in_the_refusal():
    hits = [rel for rel, s in _engine_py() if LEGACY in s]
    assert hits == ["app/contact_authority.py"], hits


# --- 3. the real CLIs ------------------------------------------------------------

def _cli_cases(tmp: Path):
    one_row = tmp / "one.csv"
    one_row.write_text(
        "candidate_id,decedent_name,county,estate_file_number,estate_status,filing_date,person_name,"
        "relationship_role,relationship_class,family_relation,source_family,source_name,source_locator,"
        "observed_at,confidence,evidence_note,operator\n"
        "cand-1,SMITH JOHN,Wake,24 E 1,OPEN,2026-01-02,MARY SMITH,EXECUTOR,LEGAL_AUTHORITY,,"
        "ESTATE_IMPORT,WAKE_CLERK,file 24 E 1,2026-08-20,HIGH,,court\n")
    (tmp / "raw").mkdir()
    extra = {
        "scripts/project_contacts_to_deedsonar.py": ["--out-dir", str(tmp / "never-created")],
        "scripts/import_operator_estate_research.py": [str(one_row)],
        "scripts/resolve_estate_household_family.py": ["--raw-dir", str(tmp / "raw")],
    }
    return [(rel, extra.get(rel, [])) for rel in sorted(GUARDED)]


def test_every_contact_cli_refuses_the_legacy_dsn_before_connecting(tmp_path):
    legacy_local = f"postgresql://postgres.{LEGACY}:pw@127.0.0.1:1/postgres"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    # DEEDSONAR_DATABASE_URL is set explicitly, so an engine .env can never supply a real DSN here.
    env.update(DEEDSONAR_DATABASE_URL=legacy_local, DATABASE_URL=legacy_local,
               PYTHONPATH=str(ROOT), LANDOS_SECRET="test")
    failures = []
    for rel, args in _cli_cases(tmp_path):
        proc = subprocess.run([sys.executable, rel, *args], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        if proc.returncode != 2 or "LEGACY_CONTACT_DSN_REFUSED" not in out or "onnection refused" in out:
            failures.append((rel, proc.returncode, out[-300:]))
    assert failures == []
    assert not (tmp_path / "never-created").exists()
