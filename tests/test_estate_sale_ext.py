"""
test_estate_sale_ext.py — text-mine estate-sale listings for motivation sub-tags.

Covers the PURE extractor (each motivation class + honest silence on routine text),
confidence (strongest tag wins, multi-class lift), the census, and the armed adapter
+ signal shape. One DB-backed case proves the LIVE census reports motivated listings
(the adapter arms) — it skips when the spine is unreachable and never writes.
"""
import pytest

from app.discovery import estate_sale_ext as es
from app.discovery.estate_sale_ext import EstateSaleExtAdapter, extract_motivations

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _r(apn, title="", description="", raw="", county="26049", listing_id=None, zip_=None):
    return {"county_fips": county, "parcel_apn": apn, "title": title,
            "description": description, "raw": raw, "listing_id": listing_id, "zip": zip_}


# ---- pure extractor --------------------------------------------------------

def test_care_move_tags():
    assert extract_motivations("Estate Sale - Assisted Living Move") == ["assisted_living"]
    assert extract_motivations("MEMORY CARE relocation, everything goes") == ["memory_care", "relocating"]
    assert "nursing_home" in extract_motivations("moving to skilled nursing")


def test_death_and_estate_tags():
    assert extract_motivations("Estate of the late John Smith") == ["deceased", "estate_settlement"]
    assert extract_motivations("Settling the estate") == ["estate_settlement"]


def test_downsize_and_urgency_tags():
    assert extract_motivations("Huge Downsizing Sale") == ["downsizing"]
    assert extract_motivations("MOVING SALE - everything must go") == ["must_go", "relocating"]
    assert extract_motivations("On-Site Estate Liquidation") == ["estate_settlement", "liquidation"]


def test_routine_text_is_silent():
    assert extract_motivations("Saturday Yard Sale - tools and toys") == []
    assert extract_motivations("") == []
    assert extract_motivations(None) == []


# ---- confidence ------------------------------------------------------------

def test_confidence_strongest_wins_and_multiclass_lifts():
    assert es._confidence(["memory_care"]) == 0.88
    assert es._confidence(["downsizing"]) == 0.68
    # care (0.85) + estate death (0.82) = two classes -> +0.03
    assert es._confidence(["assisted_living", "deceased"]) == 0.88
    assert es._confidence([]) == 0.0


# ---- census ----------------------------------------------------------------

def test_census_counts_and_covers_when_motivated():
    recs = [_r("A", title="Assisted Living Move"),
            _r("B", title="Saturday Yard Sale"),          # text, no motivation
            _r("C", title="", description="")]            # no text
    c = es.census(recs)["26049"]
    assert c["listings"] == 3 and c["with_text"] == 2 and c["motivated"] == 1
    assert c["with_apn"] == 3 and c["covered"] is True


def test_census_reports_gap_when_no_motivation():
    recs = [_r("A", title="Community Garage Sale")]
    c = es.census(recs)["26049"]
    assert c["covered"] is False and "none name a motivation" in c["gap"]


# ---- adapter ---------------------------------------------------------------

def test_adapter_emits_per_motivated_listing_and_logs_nonmatches():
    recs = [
        _r("HIT", title="Estate of the late Mary Doe - memory care move",
           listing_id="L1", zip_="49085"),
        _r("SKIP", title="Weekend Yard Sale", listing_id="L2"),   # non-match
    ]
    a = EstateSaleExtAdapter(TENANT, records=recs)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1
    s = sigs[0]
    assert s["parcel_apn"] == "HIT" and s["source_ref"] == "L1:estate_sale_ext"
    assert s["raw_payload"]["type"] == "estate_sale_ext"
    assert set(s["raw_payload"]["motivation_tags"]) == {"deceased", "estate_settlement", "memory_care"}
    assert set(s["raw_payload"]["motivation_classes"]) == {"care", "estate"}
    assert s["confidence"] >= 0.88 and s["raw_payload"]["zip"] == "49085"


def test_adapter_uses_canon_apn_when_toplevel_null():
    # loader coalesces canon_parcel_apn; here the record already carries it as parcel_apn
    a = EstateSaleExtAdapter(TENANT, records=[_r("CANON123", title="downsizing", listing_id="L3")])
    assert a.fetch()[0]["parcel_apn"] == "CANON123"


def test_adapter_skips_rows_lacking_both_listing_id_and_apn():
    # honest-null: no stable natural key -> skip + count, never a listing_text[:48]
    # silent-collide. A motivated row with an apn but no listing_id still emits (ref=apn).
    recs = [
        _r("", title="downsizing", listing_id=None),          # motivated but NO ref -> skip
        _r("HAS_APN", title="downsizing", listing_id=None),   # no listing_id but apn -> ref=apn
    ]
    a = EstateSaleExtAdapter(TENANT, records=recs)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.skipped_no_ref == 1
    assert sigs[0]["parcel_apn"] == "HAS_APN"
    assert sigs[0]["source_ref"] == "HAS_APN:estate_sale_ext"


# ---- DB-backed: live census must show motivated listings (adapter arms) -----

def test_live_census_arms_today():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            rep = es.census_live(cur, TENANT)
        assert rep, "expected estate_sale listings on the spine"
        assert sum(c["motivated"] for c in rep.values()) >= 1   # armed: motivation present
        assert sum(c["with_text"] for c in rep.values()) >= 1
    finally:
        conn.rollback()
        conn.close()
