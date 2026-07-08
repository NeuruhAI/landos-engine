"""
test_exemption_drop.py — the "sleeper" pre-probate death detector.

Covers the PURE diff (lost occupancy exemption + no sale => emit; every honest
suppression), confidence tiers, the per-county census gate, and the adapter's
disarm-vs-arm behaviour. One DB-backed case proves the LIVE census reports a gap
(no exemption column captured today) — it skips when the spine is unreachable and
never writes.
"""
import pytest

from app.discovery import exemption_drop as ed
from app.discovery.exemption_drop import ExemptionDropAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _rec(county, apn, vintages):
    return {"county_fips": county, "parcel_apn": apn, "vintages": vintages}


# ---- normalization ---------------------------------------------------------

def test_normalize_folds_homestead_and_aliases():
    assert ed.normalize_exemptions(["Homestead"]) == {"pre"}
    assert ed.normalize_exemptions(["principal-residence"]) == {"pre"}
    assert ed.normalize_exemptions(["Senior Citizen", "disabled_veteran"]) == {"senior", "disabled_vet"}
    assert ed.normalize_exemptions(["agricultural", "industrial"]) == set()   # unknown -> dropped
    assert ed.normalize_exemptions(None) == set()


# ---- pure diff: emit only on a genuine sleeper -----------------------------

def test_lost_homestead_no_sale_emits():
    rec = _rec("26049", "P1", [
        {"captured_at": "2024-04-01", "exemption_flags": ["pre"], "sale_recorded": False},
        {"captured_at": "2026-04-01", "exemption_flags": [], "sale_recorded": False}])
    ev = ed.diff_exemptions(rec)
    assert len(ev) == 1
    assert ev[0]["dropped"] == {"pre"} and ev[0]["confidence"] == 0.70
    assert ev[0]["prior"] == "2024-04-01" and ev[0]["current"] == "2026-04-01"


def test_pre_plus_senior_drop_is_strongest():
    rec = _rec("26049", "P2", [
        {"captured_at": "2024", "exemption_flags": ["pre", "senior"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": [], "sale_recorded": False}])
    ev = ed.diff_exemptions(rec)
    assert ev[0]["dropped"] == {"pre", "senior"} and ev[0]["confidence"] == 0.85


def test_sale_recorded_suppresses():
    rec = _rec("26049", "P3", [
        {"captured_at": "2024", "exemption_flags": ["pre"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": [], "sale_recorded": True}])
    assert ed.diff_exemptions(rec) == []          # a sale explains the drop — not a sleeper


def test_senior_drop_while_pre_holds_emits():
    rec = _rec("26049", "P4", [
        {"captured_at": "2024", "exemption_flags": ["pre", "senior"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": ["pre"], "sale_recorded": False}])
    # senior dropped but PRE held -> senior is person-bound, so this DOES emit
    ev = ed.diff_exemptions(rec)
    assert len(ev) == 1 and ev[0]["dropped"] == {"senior"}


def test_no_occupancy_change_no_emit():
    rec = _rec("26049", "P4b", [
        {"captured_at": "2024", "exemption_flags": ["pre"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": ["pre"], "sale_recorded": False}])
    assert ed.diff_exemptions(rec) == []


def test_non_occupancy_exemption_lost_is_ignored():
    rec = _rec("26049", "P5", [
        {"captured_at": "2024", "exemption_flags": ["agricultural"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": [], "sale_recorded": False}])
    assert ed.diff_exemptions(rec) == []          # ag leaving is not a death/move-out signal


def test_gained_exemption_no_emit():
    rec = _rec("26049", "P6", [
        {"captured_at": "2024", "exemption_flags": [], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": ["pre"], "sale_recorded": False}])
    assert ed.diff_exemptions(rec) == []


def test_not_captured_window_is_silent_never_a_false_drop():
    # a vintage WITHOUT the exemption_flags key cannot evidence absence -> no emit,
    # never a fabricated drop (the whole census-first point).
    rec = _rec("26049", "P7", [
        {"captured_at": "2024", "vested_owner": "SMITH JOHN"},              # today's shape
        {"captured_at": "2026", "vested_owner": "SMITH ESTATE"}])
    assert ed.diff_exemptions(rec) == []


# ---- census gate -----------------------------------------------------------

def test_census_reports_gap_when_column_absent():
    recs = [_rec("26049", "A", [{"captured_at": "2026", "vested_owner": "X"}])]  # no exemption_flags
    c = ed.census(recs)["26049"]
    assert c["covered"] is False and c["vintages_with_exemption_col"] == 0
    assert "NOT captured" in c["gap"] and "enrich_owner_chain" in c["first_task"]


def test_census_needs_two_vintages_to_diff():
    recs = [_rec("26049", "B", [{"captured_at": "2026", "exemption_flags": ["pre"], "sale_recorded": False}])]
    c = ed.census(recs)["26049"]
    assert c["covered"] is False and c["vintages_with_exemption_col"] == 1
    assert "<2 vintages" in c["gap"]


def test_census_covered_when_two_exemption_vintages():
    recs = [_rec("26049", "C", [
        {"captured_at": "2024", "exemption_flags": ["pre"], "sale_recorded": False},
        {"captured_at": "2026", "exemption_flags": [], "sale_recorded": False}])]
    assert ed.census(recs)["26049"]["covered"] is True


# ---- adapter: disarm vs arm ------------------------------------------------

def test_adapter_disarms_uncovered_county_emits_nothing():
    recs = [_rec("26049", "A", [{"captured_at": "2026", "vested_owner": "X"}])]  # no column
    a = ExemptionDropAdapter(TENANT, roll_records=recs)
    assert a.fetch() == []                        # disarmed, never fabricated
    assert a.last_census["26049"]["covered"] is False


def test_adapter_arms_covered_county_and_shapes_signal():
    recs = [
        _rec("26049", "HIT", [
            {"captured_at": "2024", "exemption_flags": ["pre", "senior"], "sale_recorded": False},
            {"captured_at": "2026", "exemption_flags": [], "sale_recorded": False}]),
        _rec("26049", "NODROP", [
            {"captured_at": "2024", "exemption_flags": ["pre"], "sale_recorded": False},
            {"captured_at": "2026", "exemption_flags": ["pre"], "sale_recorded": False}]),
    ]
    a = ExemptionDropAdapter(TENANT, roll_records=recs)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1  # the no-drop diffable parcel is logged
    s = sigs[0]
    assert s["parcel_apn"] == "HIT" and s["confidence"] == 0.85
    assert s["source_ref"] == "HIT:2026:exemption_drop"
    assert s["raw_payload"]["type"] == "exemption_drop"
    assert s["raw_payload"]["dropped"] == ["pre", "senior"]
    assert s["raw_payload"]["sale_recorded"] is False


# ---- DB-backed: the LIVE census must report a gap (no fake signal) ----------

def test_live_census_reports_gap_today():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            rep = ed.census_live(cur, TENANT)
        # some counties DO carry vintages today (owner-mailing capture), but NONE
        # carry the exemption column yet -> every county is an honest gap, 0 covered.
        assert rep, "expected roll vintages on the spine"
        assert all(not c["covered"] for c in rep.values())
        assert all(c["vintages_with_exemption_col"] == 0 for c in rep.values())
    finally:
        conn.rollback()
        conn.close()
