from app.free_routes.wake_delinquent import parse_line, account_from_row, summarize, index_by_pin, LAYOUT, RECORD_SIZE
from urllib.parse import unquote_plus
from app.free_routes.arcgis_cases import fetch_cases_by_parcel, count_all


def _line(**over):
    buf = [" "] * RECORD_SIZE
    def put(name, val):
        a, b = next((s, e) for n, s, e in LAYOUT if n == name)
        s = str(val)
        width = b - a + 1
        s = s.rjust(width, "0") if name in ("tape_date", "reid", "deed_book", "deed_page", "deed_date", "total_due",
                                             "total_principal_due", "total_real_value", "building_value", "land_value",
                                             "net_billable_value", "total_billed", "interest_due", "cost_due", "zip",
                                             "account_number", "tax_year") else s.ljust(width)
        buf[a - 1:b] = list(s[:width])
    vals = dict(tape_date="09012026", account_number="1234567890", tax_year="2024", reid="0116202", pin="0648080648",
                pin_ext="990", primary_owner="ADAMS, QUENTIN CARLISLE", mailing_1="605 UTLEY ST",
                mailing_2="HOLLY SPRINGS NC  27540-9169", street_number="605", street_name="UTLEY", street_type="ST",
                zip="27540", deed_book="019488", deed_page="00755", deed_date="07262024", total_real_value="00000343000",
                total_principal_due="000000020.00", total_due="000000025.00")
    vals.update(over)
    for k, v in vals.items():
        put(k, v)
    return "".join(buf)


def test_layout_covers_the_record_and_parses_money_dates_and_names():
    row = parse_line(_line())
    a = account_from_row(row)
    assert a.source_as_of == "2026-09-01" and a.tax_year == "2024" and a.reid == "0116202" and a.pin == "0648080648"
    assert a.primary_owner == "ADAMS, QUENTIN CARLISLE" and a.mailing_address == "605 UTLEY ST, HOLLY SPRINGS NC 27540-9169"
    assert a.situs_address == "605 UTLEY ST" and a.zip == "27540"
    assert a.deed_book == "19488" and a.deed_page == "755" and a.deed_date == "2024-07-26"
    assert a.total_real_value == 343000 and a.total_due == 25.0 and a.total_principal_due == 20.0


def test_unknown_dates_stay_none_and_summary_counts():
    a = account_from_row(parse_line(_line(deed_date="00000000")))
    assert a.deed_date is None
    b = account_from_row(parse_line(_line(tax_year="2023")))
    s = summarize([a, b])
    assert s["rows"] == 2 and s["distinct_pins"] == 1 and s["tax_years"] == {"2023": 1, "2024": 1}
    assert s["source_as_of"] == "2026-09-01" and s["total_due_sum"] == 50.0 and s["with_deed_ref"] == 2
    assert list(index_by_pin([a, b]).keys()) == ["0648080648"]


def test_arcgis_fetch_batches_and_converts_dates():
    calls = []
    def fake(url):
        calls.append(url)
        if "returnCountOnly" in url:
            return {"count": 3}
        return {"features": [{"attributes": {"CaseNumber": "C1", "ParcelId": "12345678", "CaseType": "Housing",
                                             "DateCreated": 1725148800000, "DateClosed": None, "CaseStatus": "Open"}}]}
    rows = fetch_cases_by_parcel("https://svc/0", ["12345678", "12345678", "9"] + [str(i) for i in range(150)],
                                 batch=100, delay_s=0, fetch=fake)
    assert len(calls) == 2 and all("ParcelId IN" in unquote_plus(c) for c in calls)
    assert rows[0]["DateCreated"] == "2024-09-01" and rows[0]["DateClosed"] is None
    assert count_all("https://svc/0", fetch=fake) == 3
