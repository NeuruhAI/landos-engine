"""
test_attorney_ranking.py — rank estate attorneys by case volume (trust-node channel).

Covers PURE ranking (aggregation, dedup, ordering), confidence, the census GATE
(t5 1.4 delivered?), and adapter disarm-vs-arm. One DB-backed case proves the LIVE
census reports the gate unmet today (1 attorney) — skips when unreachable.
"""
import pytest

from app.discovery import attorney_ranking as ar
from app.discovery.attorney_ranking import AttorneyRankingAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _c(attorney, case_ref, case_type="probate_in", parcel_apn=None):
    return {"attorney": attorney, "case_ref": case_ref, "case_type": case_type, "parcel_apn": parcel_apn}


# ---- ranking ---------------------------------------------------------------

def test_rank_orders_by_case_volume_and_dedups():
    # case/whitespace variants normalize to the same attorney; c2 appears twice -> dedup
    recs = [_c("Smith, Jane", "c1"), _c("SMITH, JANE", "c2"), _c("smith,  jane", "c2"),
            _c("Doe, John", "c3")]
    ranked = ar.rank_attorneys(recs)
    assert ranked[0]["attorney_name"] == "Smith, Jane" and ranked[0]["case_count"] == 2
    assert ranked[0]["rank"] == 1 and ranked[1]["rank"] == 2


def test_records_without_attorney_ignored():
    assert ar.rank_attorneys([_c(None, "c1"), _c("", "c2")]) == []


def test_case_types_and_parcels_collected():
    recs = [_c("Roe, Amy", "c1", case_type="probate_in", parcel_apn="P1"),
            _c("Roe, Amy", "c2", case_type="lis_pendens", parcel_apn="P2")]
    r = ar.rank_attorneys(recs)[0]
    assert r["case_types"] == ["lis_pendens", "probate_in"] and r["parcels"] == ["P1", "P2"]


# ---- confidence ------------------------------------------------------------

def test_confidence_scales_with_volume_and_rank():
    assert ar._confidence(6, 1) == 0.90            # 0.55+0.30+0.05 capped
    assert ar._confidence(1, 3) == 0.60
    assert ar._confidence(6, 1) > ar._confidence(1, 3)


# ---- census gate -----------------------------------------------------------

def test_census_reports_gate_unmet_with_one_attorney():
    recs = [_c("Hesser, Randall Gary", "c1")]     # today's live shape
    c = ar.census(recs)
    assert c["covered"] is False and c["distinct_attorneys"] == 1
    assert "t5 1.4" in c["gate"] and "need >= 2" in c["gap"]


def test_census_covered_with_rankable_distribution():
    recs = [_c("Smith, Jane", "c1"), _c("Smith, Jane", "c2"), _c("Doe, John", "c3")]
    c = ar.census(recs)
    assert c["covered"] is True and c["distinct_attorneys"] == 2 and c["top_case_count"] == 2


# ---- adapter ---------------------------------------------------------------

def test_adapter_disarms_when_gate_unmet():
    a = AttorneyRankingAdapter(TENANT, records=[_c("Hesser, Randall Gary", "c1")])
    assert a.fetch() == [] and a.last_census["covered"] is False


def test_adapter_arms_and_shapes_entity_signal():
    recs = [_c("Smith, Jane", "c1", parcel_apn="P1"), _c("Smith, Jane", "c2", parcel_apn="P2"),
            _c("Doe, John", "c3")]
    a = AttorneyRankingAdapter(TENANT, records=recs)
    sigs = a.fetch()
    assert len(sigs) == 2
    top = sigs[0]
    assert top["parcel_apn"] is None                  # entity signal, not parcel-keyed
    assert top["source_ref"] == "Smith, Jane:attorney_ranking"
    assert top["owner_name"] == "Smith, Jane"
    assert top["raw_payload"]["type"] == "attorney_ranking"
    assert top["raw_payload"]["rank"] == 1 and top["raw_payload"]["case_count"] == 2
    assert top["raw_payload"]["parcels"] == ["P1", "P2"]


# ---- DB-backed live gate ---------------------------------------------------

def test_live_census_reports_gate_unmet_today():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            rep = ar.census_live(cur, TENANT)
        # exactly the census finding: attorney-of-record too thin to rank
        assert rep["covered"] is False
        assert rep["distinct_attorneys"] < 2 and "t5 1.4" in rep["gate"]
    finally:
        conn.rollback()
        conn.close()
