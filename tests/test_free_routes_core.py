import json, os
from app.free_routes.receipts import RouteReceipt, write_receipt, halted_receipt
from app.free_routes.registry import ROUTES, FREE_ROUTES_EXHAUSTED_REQUIRES, render_markdown, routes_by_mode
from app.free_routes.human_routes import (t2_notice_search, t4_obituary_search, t7_rod_search, t10_people_search,
                                          t5_sos_search, write_worklist, WORKLIST_COLUMNS)
from app.free_routes.obituary import parse_survived_by
from app.free_routes.routing_order import route_order_for


def test_register_names_every_founder_route_with_terms_and_a_writer():
    ids = [r.route_id for r in ROUTES]
    assert ids == ["T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","I1","I2","I3","I4","I5"]
    for r in ROUTES:
        assert r.license and r.writer and r.adapter and r.provenance_url
        if r.mode != "AUTOMATED":
            assert r.halt in ("H-11", "H-12") and r.human_step, r.route_id
    assert FREE_ROUTES_EXHAUSTED_REQUIRES == tuple(f"T{i}" for i in range(1, 11))
    md = render_markdown()
    assert "exhaust the ability to do stuff for free" in md and "| T10 |" in md


def test_manual_routes_are_the_ones_whose_terms_forbid_scripts():
    manual = {r.route_id for r in routes_by_mode("MANUAL")}
    assert manual == {"T1", "T2", "T3", "T4", "T5", "T7", "T10"}


def test_receipt_hit_rate_and_file(tmp_path):
    r = RouteReceipt("T8", "open data", "AUTOMATED", "Wake", "public", "https://x", rows_tried=200, hits=50,
                     source_as_of="2026-09-01")
    assert r.hit_rate() == 0.25
    p = write_receipt(r, str(tmp_path))
    d = json.load(open(p))
    assert d["hit_rate"] == 0.25 and d["source_as_of"] == "2026-09-01" and d["halted"] is False
    assert os.path.exists(tmp_path / "SHA256SUMS")
    h = halted_receipt("T2", "notices", mode="MANUAL", source="ncnotices", license="ToS §3", provenance_url="https://x",
                       halt_code="H-12", human_step="search", rows_tried=40)
    assert h.halted and h.halt_code == "H-12" and h.hit_rate() == 0.0


def test_human_route_urls_carry_the_exact_terms():
    t2 = t2_notice_search("SMITH, JOHN A", "Wake")
    assert t2["url"].endswith("Search.aspx") and t2["keywords"] == '"SMITH JOHN A"' and t2["county"] == "Wake"
    t4 = t4_obituary_search("John A Smith", "Raleigh")
    assert "firstName=John" in t4["legacy_url"] and "lastName=Smith" in t4["legacy_url"] and "survived+by" in t4["google_url"]
    assert t7_rod_search("Smith", "Mecklenburg")["url"].startswith("https://meckrod")
    assert t7_rod_search("Smith", "Orange")["url"] == ""
    p = t10_people_search("John Smith", "Cary")
    assert p["fastpeoplesearch"].endswith("/name/john-smith_cary-nc") and "truepeoplesearch" in p["truepeoplesearch"]
    assert t5_sos_search("ACME HOLDINGS LLC")["entity_name"] == "ACME HOLDINGS LLC"


def test_worklist_is_written_0600_with_the_fixed_columns(tmp_path):
    p = tmp_path / "wl.csv"
    n = write_worklist(str(p), [{"route_id": "T2", "global_rank": "1", "candidate_id": "c1", "name": "X", "url": "u"}])
    assert n == 1 and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert open(p).readline().strip() == ",".join(WORKLIST_COLUMNS)


def test_obituary_parser_extracts_named_heirs_only():
    text = ("John A. Smith, 84, of Cary, passed away. He is survived by his wife, Mary Smith of Cary; "
            "sons John Smith Jr. (Ann) of Raleigh and Paul Smith of Durham, NC; daughter Susan Jones of Apex; "
            "and three grandchildren.")
    heirs = parse_survived_by(text)
    names = {h["name"]: h for h in heirs}
    assert "Mary Smith" in names and names["Mary Smith"]["relationship"] == "wife" and names["Mary Smith"]["city"] == "Cary"
    assert "John Smith Jr." in names and names["John Smith Jr."]["relationship"] == "son"
    assert "Paul Smith" in names and names["Paul Smith"]["city"].startswith("Durham")
    assert "Susan Jones" in names and names["Susan Jones"]["relationship"] == "daughter"
    assert all(h["confidence"] == "obituary" for h in heirs)
    assert not any("grandchildren" in h["name"].lower() for h in heirs)
    assert parse_survived_by("") == []


def test_route_order_follows_the_why_components():
    assert route_order_for(["probate_signal", "tax_distress"], "DECEASED")[:5] == ["T1", "T2", "T3", "T4", "I3"]
    assert route_order_for(["entity_owner"], "", "ENTITY")[:3] == ["T5", "T8", "T7"]
    assert route_order_for(["out_of_state"])[:2] == ["I1", "T7"]
    order = route_order_for([])
    assert order == ["T6", "T8", "T9", "T7", "T10"] and len(set(order)) == len(order)
