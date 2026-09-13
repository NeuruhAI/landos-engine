"""The engine's ONE database is the DeedSonar production authority; the retired spine is refused.

app/config.py resolves DATABASE_URL (the pool DSN every writer shares — /evaluate,
hunt, recipe, sync_to_crm) from DEEDSONAR_DATABASE_URL first and the legacy-named
DATABASE_URL only as a fallback. Whichever wins, the retired spine
lvdimujkfheeyqchjzra is refused at import time (LEGACY_SPINE_REFUSED), so a stale
.env can no longer put the scorer back on lvdi — which is how efxo landos.scores
fell 47 rows behind lvdi between 2026-09-10 and 2026-09-12.

No test connects to any database: every DSN points at 127.0.0.1:1, and importing
app.config opens nothing (the pool lives in app.db).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.contact_authority import CANONICAL_REF, RETIRED_REFS

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "lvdimujkfheeyqchjzra"
POOLER_LEGACY = f"postgresql://postgres.{LEGACY}:pw@127.0.0.1:1/postgres"
DIRECT_LEGACY = f"postgresql://postgres:pw@db.{LEGACY}.supabase.co:5432/postgres"
POOLER_CANONICAL = f"postgresql://postgres.{CANONICAL_REF}:pw@127.0.0.1:1/postgres"
LOCAL_DSN = "postgresql://court:court@127.0.0.1:1/court"

# app.config resolves DATABASE_URL at import. A checkout without a .env (a fresh
# worktree, CI) would otherwise fail collection; give the import a canonical-shaped
# dead-port DSN ONLY in that case, so a deployed .env is never overridden.
if not (ROOT / ".env").exists():
    os.environ.setdefault("DEEDSONAR_DATABASE_URL", POOLER_CANONICAL)
    os.environ.setdefault("LANDOS_SECRET", "x")

from app.config import ALLOW_LEGACY_SPINE_VAR, refuse_legacy_spine, resolve_engine_database_url  # noqa: E402


# ---- the resolver -----------------------------------------------------------

def test_canonical_variable_wins_over_legacy_named_fallback():
    assert resolve_engine_database_url({"DEEDSONAR_DATABASE_URL": POOLER_CANONICAL,
                                        "DATABASE_URL": LOCAL_DSN}) == POOLER_CANONICAL


def test_fallback_only_when_canonical_absent_or_placeholder():
    assert resolve_engine_database_url({"DATABASE_URL": LOCAL_DSN}) == LOCAL_DSN
    for placeholder in ("", "replace-me", "postgresql://postgres:[YOUR-PASSWORD]@db.x.supabase.co/postgres"):
        assert resolve_engine_database_url({"DEEDSONAR_DATABASE_URL": placeholder,
                                            "DATABASE_URL": LOCAL_DSN}) == LOCAL_DSN


def test_missing_both_is_a_clear_error():
    with pytest.raises(RuntimeError) as e:
        resolve_engine_database_url({})
    assert "DATABASE_URL is missing" in str(e.value)


@pytest.mark.parametrize("var", ["DEEDSONAR_DATABASE_URL", "DATABASE_URL"])
@pytest.mark.parametrize("dsn", [POOLER_LEGACY, DIRECT_LEGACY])
def test_retired_spine_is_refused_from_either_variable(var, dsn):
    with pytest.raises(RuntimeError) as e:
        resolve_engine_database_url({var: dsn})
    assert str(e.value).startswith("LEGACY_SPINE_REFUSED")
    assert CANONICAL_REF in str(e.value)


def test_retired_spine_is_refused_when_smuggled_through_pg_env():
    with pytest.raises(RuntimeError, match="LEGACY_SPINE_REFUSED"):
        resolve_engine_database_url({"DEEDSONAR_DATABASE_URL": "postgresql:///postgres",
                                     "PGHOST": f"db.{LEGACY}.supabase.co"})
    with pytest.raises(RuntimeError, match="LEGACY_SPINE_REFUSED"):
        refuse_legacy_spine("dbname=postgres", {"PGUSER": f"postgres.{LEGACY}"})


def test_forensic_read_override_is_explicit_and_named():
    env = {"DATABASE_URL": POOLER_LEGACY, ALLOW_LEGACY_SPINE_VAR: "1"}
    assert resolve_engine_database_url(env) == POOLER_LEGACY
    assert ALLOW_LEGACY_SPINE_VAR == "LANDOS_ALLOW_LEGACY_SPINE"
    assert set(RETIRED_REFS) == {LEGACY}


# ---- the real import: app.config at process start ---------------------------

def _import_config(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in {"DEEDSONAR_DATABASE_URL", "DATABASE_URL", "PGHOST", "PGUSER", ALLOW_LEGACY_SPINE_VAR}}
    # pin every DB variable so the checkout's .env (load_dotenv, no override) cannot fill one in
    env.update({"DEEDSONAR_DATABASE_URL": "", "DATABASE_URL": "", "LANDOS_SECRET": "x"})
    env.update(env_overrides)
    code = "import app.config as c; print('HOST=' + c.DATABASE_URL.rsplit('@', 1)[-1])"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_process_start_refuses_a_stale_legacy_database_url():
    r = _import_config({"DATABASE_URL": POOLER_LEGACY})
    assert r.returncode != 0
    assert "LEGACY_SPINE_REFUSED" in r.stderr
    assert "connection" not in r.stderr.lower()


def test_process_start_prefers_the_canonical_variable_over_a_legacy_fallback():
    # the deployed shape: DEEDSONAR_DATABASE_URL = efxo, DATABASE_URL = lvdi (backup_spine's forensic value)
    r = _import_config({"DEEDSONAR_DATABASE_URL": POOLER_CANONICAL, "DATABASE_URL": POOLER_LEGACY})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "HOST=127.0.0.1:1/postgres"


def test_process_start_forensic_override_still_requires_the_named_flag():
    r = _import_config({"DATABASE_URL": POOLER_LEGACY, ALLOW_LEGACY_SPINE_VAR: "1"})
    assert r.returncode == 0, r.stderr


_DIRECT_ENV_READ = re.compile(r"""environ(\[["']DATABASE_URL["']\]|\.get\(["']DATABASE_URL["'])|getenv\(["']DATABASE_URL["']""")


def test_no_engine_code_reads_database_url_around_the_resolver():
    offenders = [str(p.relative_to(ROOT)) for p in [*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
                 if p.name != "config.py" and _DIRECT_ENV_READ.search(p.read_text())]
    assert not offenders, offenders
