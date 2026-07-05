"""
test_sync_owner_projection.py — ENRICH-1 P1: the owner_name projection idiom.

sync_to_crm projects owner_name EXISTING-FIRST (the asset_class idiom):
enrichment fills blanks (NULL or '') and NEVER overwrites a name already on
the board (manually entered or previously projected). The test exercises the
EXACT SQL fragment the sync runs — single-sourced from
scripts.sync_to_crm.OWNER_NAME_PROJECTION_SQL — against a TEMP table on the
real database, because the semantics live in Postgres, not Python.
Skips (never false-greens) when the database is unreachable.
"""
import pytest

from scripts.sync_to_crm import OWNER_NAME_PROJECTION_SQL


@pytest.fixture()
def cur():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001 — any connect failure means skip, not fail
        pytest.skip(f"database unreachable: {e}")
    c = conn.cursor()
    c.execute("create temp table t_owner_proj (id int primary key, owner_name text)")
    yield c
    conn.rollback()   # nothing this test does ever lands
    conn.close()


def _project(cur, row_id, enriched):
    """Run the projection fragment exactly as sync_to_crm does."""
    cur.execute(
        f"update t_owner_proj set {OWNER_NAME_PROJECTION_SQL} where id = %s",
        (enriched, row_id))


def _get(cur, row_id):
    cur.execute("select owner_name from t_owner_proj where id = %s", (row_id,))
    return cur.fetchone()[0]


# --- direction 1: enrichment FILLS blanks -----------------------------------

def test_fills_null_blank(cur):
    cur.execute("insert into t_owner_proj values (1, null)")
    _project(cur, 1, "SMITH JOHN A")
    assert _get(cur, 1) == "SMITH JOHN A"


def test_fills_empty_string_blank(cur):
    cur.execute("insert into t_owner_proj values (2, '')")
    _project(cur, 2, "SMITH JOHN A")
    assert _get(cur, 2) == "SMITH JOHN A"


# --- direction 2: enrichment NEVER overwrites an existing name ---------------

def test_never_overwrites_existing_name(cur):
    cur.execute("insert into t_owner_proj values (3, 'Manually Entered Name')")
    _project(cur, 3, "COUNTY GIS VALUE")
    assert _get(cur, 3) == "Manually Entered Name"


def test_null_enrichment_keeps_existing(cur):
    cur.execute("insert into t_owner_proj values (4, 'Keep Me')")
    _project(cur, 4, None)
    assert _get(cur, 4) == "Keep Me"


def test_null_enrichment_leaves_blank_null(cur):
    # no name anywhere -> stays honestly NULL (never a fabricated value)
    cur.execute("insert into t_owner_proj values (5, null)")
    _project(cur, 5, None)
    assert _get(cur, 5) is None
