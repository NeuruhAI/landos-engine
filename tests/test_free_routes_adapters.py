from datetime import datetime, timezone
from app.free_routes.attempts import Attempt, write_attempts
from app.free_routes.estate_rows import norm_pin, index_by_pin, is_out_of_state, is_entity_owner, is_probate
from app.free_routes.t8_open_data import match_wake, match_charlotte_code, latest_wake_extract_name
from app.free_routes.invented import i1_out_of_state, i3_heir_cross_match, i4_route_orders, I2_COLLAPSE_SQL
from app.free_routes.wake_delinquent import LAYOUT, RECORD_SIZE

NOW = "2026-09-02T19:30:00Z"


def _wake_line(pin, owner="DOE, JANE", total_due="000000100.00", year="2025"):
    buf = [" "] * RECORD_SIZE
    def put(name, val, num=False):
        a, b = next((s, e) for n, s, e in LAYOUT if n == name); w = b - a + 1
        s = str(val).rjust(w, "0") if num else str(val).ljust(w); buf[a - 1:b] = list(s[:w])
    put("tape_date", "09012026"); put("account_number", "1", True); put("tax_year", year); put("reid", "1", True); put("pin", pin)
    put("primary_owner", owner); put("mailing_1", "1 MAIN ST"); put("mailing_2", "RALEIGH NC 27601"); put("deed_book", "12345", True); put("deed_page", "678", True)
    put("total_real_value", "100000", True); put("total_due", total_due); put("total_principal_due", "000000090.00")
    return "".join(buf)


def test_attempts_dry_run_counts_and_apply_writes(tmp_path):
    atts = [Attempt("c1", "T8", "run1", "HIT", "WAKE_TAX_DELQ853", "2026-09-01", NOW), Attempt("c2", "T8", "run1", "NO_HIT", "WAKE_TAX_DELQ853", "2026-09-01", NOW)]
    dry = write_attempts(atts, database_url=None, apply=False)
    assert dry["attempts"] == 2 and dry["written"] == 0 and dry["by_outcome"] == {"HIT": 1, "NO_HIT": 1}
    executed = []
    class Cur:
        def execute(self, sql, params): executed.append(params)
        def executemany(self, sql, seq): executed.extend(seq)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Conn:
        def cursor(self): return Cur()
        def commit(self): executed.append("COMMIT")
        def __enter__(self): return self
        def __exit__(self, *a): return False
    out = write_attempts(atts, database_url="postgresql://x", apply=True, connect=lambda url: Conn())
    assert out["written"] == 2 and "COMMIT" in executed and executed[0]["route_id"] == "T8" and executed[0]["outcome"] == "HIT"


def test_wake_match_joins_on_pin_and_stamps_the_tape_date(tmp_path):
    p = tmp_path / "REAL_ESTATE_DELQ853_09012026.txt"
    p.write_text(_wake_line("0648080648") + "\n" + _wake_line("0648080648", year="2024", total_due="000000050.00") + "\n" + _wake_line("1111111111") + "\n", encoding="latin-1")
    estate = [{"candidate_id": "c-hit", "fips": "37183", "county": "Wake", "apn": "0648080648", "owner_name": "DOE, JANE"},
              {"candidate_id": "c-miss", "fips": "37183", "county": "Wake", "apn": "9999999999", "owner_name": "X"},
              {"candidate_id": "c-meck", "fips": "37119", "county": "Mecklenburg", "apn": "12345678", "owner_name": "Y"}]
    out = match_wake(estate, str(p), run_id="run1", now_iso=NOW, receipt_path="r.json")
    assert out["receipt"].rows_tried == 2 and out["receipt"].hits == 1 and out["receipt"].source_as_of == "2026-09-01"
    sig = out["signals"][0]
    assert sig["candidate_id"] == "c-hit" and sig["total_due"] == 150.0 and sig["tax_years"] == "2024;2025" and sig["years_due_count"] == 2
    assert sig["deed_book"] == "12345" and sig["deed_page"] == "678" and sig["mailing_address_on_bill"].startswith("1 MAIN ST")
    outcomes = {a.import_row_id: a.outcome for a in out["attempts"]}
    assert outcomes == {"c-hit": "HIT", "c-miss": "NO_HIT"}
    assert latest_wake_extract_name(datetime(2026, 9, 2, tzinfo=timezone.utc)) == "real_estate_delq853_09022026.zip"


def test_charlotte_match_uses_the_service_by_parcel():
    def fake(url):
        return {"features": [{"attributes": {"CaseNumber": "C1", "ParcelId": "12345678", "CaseType": "Housing", "CaseStatus": "Closed", "DateCreated": 1725148800000}}]}
    estate = [{"candidate_id": "m1", "fips": "37119", "apn": "12345678"}, {"candidate_id": "m2", "fips": "37119", "apn": "87654321"}]
    out = match_charlotte_code(estate, run_id="r", now_iso=NOW, receipt_path=None, fetch=fake, delay_s=0)
    assert out["receipt"].hits == 1 and out["receipt"].rows_tried == 2
    assert out["signals"][0]["source_as_of"] == "2024-09-01" and {a.outcome for a in out["attempts"]} == {"HIT", "NO_HIT"}


def test_invented_routes():
    estate = [{"candidate_id": "a", "owner_name": "SMITH JOHN", "mailing_state": "FL", "mailing_address": "1 Palm", "mailing_city": "Tampa", "mailing_zip": "33601", "county": "Wake", "global_rank": "5", "primary_recipe": "probate_heir", "qualification_state": "QUALIFIED", "owner_condition": "DECEASED"},
              {"candidate_id": "b", "owner_name": "ACME HOLDINGS LLC", "mailing_state": "NC", "county": "Mecklenburg", "global_rank": "9", "primary_recipe": "entity_absentee", "qualification_state": "QUALIFIED"}]
    i1 = i1_out_of_state(estate, run_id="r", now_iso=NOW, receipt_path=None)
    assert i1["receipt"].hits == 1 and i1["worklist"][0]["candidate_id"] == "a" and "GSCCCA" not in i1["worklist"][0]["instructions"] and "FL" in i1["worklist"][0]["instructions"]
    i3 = i3_heir_cross_match(estate, [{"candidate_id": "a", "party_name": "Mary Smith", "role": "HEIR", "city": "Cary"}], run_id="r", now_iso=NOW, receipt_path=None)
    assert i3["worklist"][0]["url"].startswith("https://www.truepeoplesearch.com/") and i3["attempts"][0].route_id == "I3"
    orders = i4_route_orders(estate)
    assert orders["a"][:2] == ["T1", "T2"] and orders["b"][:1] == ["T5"]
    assert "candidate_parties" in I2_COLLAPSE_SQL
    assert is_out_of_state(estate[0]) and is_entity_owner(estate[1]) and is_probate(estate[0])
    assert norm_pin("0648-08-0648.990") == "0648080648" and list(index_by_pin([{"apn": "0648080648"}]).keys()) == ["0648080648"]


def test_estate_rows_take_owner_class_from_the_resolution_ledger(tmp_path):
    from pathlib import Path
    from app.free_routes.estate_rows import load_estate_rows
    (tmp_path / "Wake" / "master").mkdir(parents=True)
    (tmp_path / "Wake" / "master" / "wake_master_0001.csv").write_text("candidate_id,owner_name,apn,global_rank\nc1,SMITH JOHN,0648080648,1\nc2,ACME LLC,0648080649,2\nc3,DOE JANE,0648080650,3\n")
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "CANDIDATE_CONTACT_RESOLUTION.csv").write_text("candidate_id,owner_class,contact_resolution_state\nc1,DECEASED,DECEASED_OWNER_NEEDS_HEIR_IDENTITY\nc2,LLC,ENTITY_NEEDS_LAWFUL_SOS_IMPORT\nc3,LIVING_PERSON,PHONE_APPENDED_HIGH\n")
    rows = load_estate_rows(tmp_path, ["Wake"], tmp_path / "bundle")
    assert [is_probate(r) for r in rows] == [True, False, False]
    assert [is_entity_owner(r) for r in rows] == [False, True, False]
    assert rows[0]["res_owner_class"] == "DECEASED" and rows[0]["fips"] == "37183"


def test_entity_fallback_uses_word_boundaries_not_substrings():
    # without a resolution-ledger class the roll name is classified by owner_route_class;
    # the old substring test called these people entities (VINCENT~INC, BANKS~BANK, CORPENING~CORP)
    people = ["VINCENT MARY", "BANKS ROBERT L", "CORPENING JAMES", "LINCOLN SARAH", "TRUSTY ANN"]
    assert [is_entity_owner({"owner_name": n}) for n in people] == [False] * len(people)
    entities = ["ACME HOLDINGS LLC", "SMITH FAMILY TRUST", "FIRST BAPTIST CHURCH", "WELLS FARGO BANK NA", "OAK PROPERTIES INC"]
    assert [is_entity_owner({"owner_name": n}) for n in entities] == [True] * len(entities)
    assert is_entity_owner({"owner_name": ""}) is False and is_entity_owner({}) is False
    # the ledger class still wins over the name
    assert is_entity_owner({"owner_name": "ACME LLC", "res_owner_class": "LIVING_PERSON"}) is False
