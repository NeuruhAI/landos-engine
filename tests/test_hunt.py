"""
test_hunt.py — HUNT-V2 §6: coverage-miss -> uncovered; dry-run writes nothing;
the GO gate refuses anything but the literal token; receipts chain per stage;
territory resolution never guesses.

DB-backed cases skip (never false-green) when the database is unreachable and
clean up every row they create.
"""
import uuid

import pytest

from app.fips import (
    parse_territory_string,
    resolve_county,
    resolve_geography,
    resolve_hunt_inputs,
    resolve_zip,
)
from app.models import HuntRequest
from app import hunt_core


# ---- territory resolution (never guessed) ------------------------------------

def test_fips_resolution_exact():
    t = parse_territory_string("Travis County, TX")
    assert t == {"fips": "48453", "state": "TX", "county": "Travis County"}
    assert resolve_county(state="MI", county="st. clair")["fips"] == "26147"


def test_fips_resolution_refuses_unknown():
    with pytest.raises(ValueError):
        resolve_county(state="TX", county="Definitely Not A County")
    with pytest.raises(ValueError):
        resolve_county(fips="99999")


def test_resolve_zip_49022_is_berrien_mi():
    """Fixture ZIP 49022 → Berrien County MI. Data row, not a product default."""
    assert resolve_zip("49022") == [
        {"fips": "26021", "state": "MI", "county": "Berrien County"}
    ]
    assert resolve_geography(zip_code="49022") == resolve_zip("49022")
    assert resolve_geography(kind="zip", value="49022") == resolve_zip("49022")


def test_multi_territory_sw_michigan_counties():
    """Explicit multi-fips territory: Berrien + Cass + Van Buren MI."""
    want = [
        {"fips": "26021", "state": "MI", "county": "Berrien County"},
        {"fips": "26027", "state": "MI", "county": "Cass County"},
        {"fips": "26159", "state": "MI", "county": "Van Buren County"},
    ]
    fipses = ["26021", "26027", "26159"]
    assert [resolve_county(fips=f) for f in fipses] == want
    assert resolve_geography(counties=fipses) == want
    assert resolve_geography(kind="counties", counties=[{"fips": f} for f in fipses]) == want
    assert resolve_hunt_inputs(territory=[{"fips": f} for f in fipses]) == want


def test_hunt_create_geography_or_territory():
    """POST /hunt accepts territory[] as today and/or geography that resolves
    through resolve_geography. Empty input does not fall back to Berrien/NC."""
    via_territory = resolve_hunt_inputs(
        territory=[{"fips": "26021"}, {"fips": "26027"}, {"fips": "26159"}]
    )
    via_zip = resolve_hunt_inputs(geography={"zip": "49022"})
    via_both = resolve_hunt_inputs(
        territory=[{"fips": "26027"}],
        geography={"zip": "49022"},
    )
    assert via_zip == [{"fips": "26021", "state": "MI", "county": "Berrien County"}]
    assert [e["county"] for e in via_territory] == [
        "Berrien County", "Cass County", "Van Buren County"
    ]
    assert [e["fips"] for e in via_both] == ["26027", "26021"]
    with pytest.raises(ValueError, match="territory or geography"):
        resolve_hunt_inputs(territory=[], geography=None)
    req = HuntRequest(geography={"kind": "zip", "value": "49022"})
    assert req.territory == []
    assert resolve_hunt_inputs(req.territory, req.geography) == via_zip
    # existing required-shape still parses
    legacy = HuntRequest(territory=[{"state": "TX", "county": "Travis"}])
    assert resolve_hunt_inputs(legacy.territory, legacy.geography) == [
        {"fips": "48453", "state": "TX", "county": "Travis County"}
    ]


def test_resolve_zip_unknown_is_not_guessed():
    with pytest.raises(ValueError):
        resolve_zip("00000")
    with pytest.raises(ValueError):
        resolve_geography(kind="nope", value="49022")


def test_staged_key_matches_ingest_convention():
    assert hunt_core.staged_key("St. Clair County") == "saint_clair"
    assert hunt_core.staged_key("Presque Isle County") == "presque_isle"
    assert hunt_core.staged_key("Van Buren County") == "van_buren"
    assert hunt_core.staged_key("Bay County") == "bay"


# ---- receipts chain per stage (the /lead-receipt idiom) -----------------------

def test_receipts_chain_genesis_and_linkage():
    job = {"receipts": []}
    r1 = hunt_core.append_receipt(job, "census", {"n": 1})
    r2 = hunt_core.append_receipt(job, "stage", {"n": 2})
    r3 = hunt_core.append_receipt(job, "dry_run_mint", {"n": 3})
    assert r1["prev"] is None and r2["prev"] == r1["chain_hash"] \
        and r3["prev"] == r2["chain_hash"]
    assert len({r1["chain_hash"], r2["chain_hash"], r3["chain_hash"]}) == 3
    # tamper-evidence: same stage name, different body -> different hashes
    other = {"receipts": []}
    o1 = hunt_core.append_receipt(other, "census", {"n": 999})
    assert o1["body_hash"] != r1["body_hash"] and o1["chain_hash"] != r1["chain_hash"]


# ---- DB-backed: census verdicts, dry-run purity, the GO gate -------------------

@pytest.fixture()
def db():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    yield conn
    conn.rollback()
    conn.close()


def test_coverage_miss_is_uncovered_with_reason(db):
    # Weston County, WY: no staged file, no signals, no known WY feed -> UNCOVERED
    entry = resolve_county(fips="56045")
    with db.cursor() as cur:
        c = hunt_core.census_county(cur, entry, ["taxsale"])
    assert c["covered"] is False
    assert "no $0 evidence" in c["reason"]
    db.rollback()


def test_covered_requires_evidence_not_feed_knowledge(db):
    # A known feed alone must not cover: fabricate an MI county entry whose name
    # matches no staged file and whose fips has no signals.
    entry = {"fips": "26XX0", "state": "MI", "county": "No Such County"}
    with db.cursor() as cur:
        c = hunt_core.census_county(cur, entry, ["taxsale"])
    assert c["covered"] is False and c["known_feeds"], \
        "MI has a known feed, but knowledge alone must never cover"
    db.rollback()


def test_dry_run_mint_writes_nothing(db):
    from scripts import mint_taxsale_leads
    with db.cursor() as cur:
        cur.execute("select count(*) as n from landos.owner_leads")
        before = cur.fetchone()["n"]
    stats = mint_taxsale_leads.main(dry_run=True, only_fips=["48491"])
    assert stats["dry_run"] is True
    with db.cursor() as cur:
        cur.execute("select count(*) as n from landos.owner_leads")
        assert cur.fetchone()["n"] == before


def test_go_gate_refuses_anything_but_GO(db):
    """A parked job fired with the wrong token aborts UNTOUCHED — and the mint
    path (the single write route into owner_leads) is never reached."""
    from app import hunt_mint
    job_id = None
    try:
        with db.cursor() as cur:
            cur.execute(
                """insert into landos.hunt_jobs (tenant_id, territory, recipe, status, counts)
                   values (%s, '[{"fips":"48491","state":"TX","county":"Williamson County"}]'::jsonb,
                           '{"signal_types":["taxsale"]}'::jsonb, 'awaiting_operator_mint',
                           '{"census":{"48491":{"covered":true}}}'::jsonb)
                   returning id""", (hunt_core.TENANT,))
            job_id = str(cur.fetchone()["id"])
        db.commit()

        with db.cursor() as cur:
            cur.execute("select count(*) as n from landos.owner_leads")
            before = cur.fetchone()["n"]
        rc = hunt_mint.main(job_id, typed="go")   # wrong case — must abort
        assert rc == 1
        with db.cursor() as cur:
            cur.execute("select count(*) as n from landos.owner_leads")
            assert cur.fetchone()["n"] == before
            cur.execute("select status from landos.hunt_jobs where id = %s", (job_id,))
            assert cur.fetchone()["status"] == "awaiting_operator_mint"
    finally:
        if job_id:
            with db.cursor() as cur:
                cur.execute("delete from landos.hunt_jobs where id = %s", (job_id,))
            db.commit()


def test_worker_refuses_non_queued_jobs(db):
    from app import hunt_worker
    job_id = None
    try:
        with db.cursor() as cur:
            cur.execute(
                """insert into landos.hunt_jobs (tenant_id, territory, status)
                   values (%s, '[{"fips":"26017","state":"MI","county":"Bay County"}]'::jsonb,
                           'synced') returning id""", (hunt_core.TENANT,))
            job_id = str(cur.fetchone()["id"])
        db.commit()
        with pytest.raises(SystemExit):
            hunt_worker.main(job_id=job_id)
    finally:
        if job_id:
            with db.cursor() as cur:
                cur.execute("delete from landos.hunt_jobs where id = %s", (job_id,))
            db.commit()
