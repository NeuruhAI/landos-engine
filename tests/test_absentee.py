"""
test_absentee.py — unit tests for the Owner-Outreach Engine (Engine 1).

Covers (all pure, no DB / no network):
  - absentee_tier logic (all four tiers + unknown + vacant-lot guard)
  - owner_type classification (individual / entity / govt)
  - absentee_score (tier weighting, entity premium, govt disposition floor)
  - signal_overlap join structure
  - final_lead_score multiplicative fusion (bumps, cap, clamp)
  - score_lead end-to-end
  - ReportAll APN mapping + normalize() (real-data-only, no fabrication on miss)
  - CSV export helpers
  - GATED skip-trace stub (Tracerfy disabled by default, no call, cost message)
  - GATED APG push stub (disabled default, destination unconfirmed raises,
    builder_buyers hard-blocked, no buyer fields in mapping)
"""
from app.score_absentee import (
    COUNTY_GEO, classify_owner_type, classify_absentee_tier, absentee_score,
    absentee_flag, build_signal_overlap, overlap_multiplier, final_lead_score,
    raw_fused_score, tiebreak_key, score_lead, TIER_BASE, GOVT_DISPOSITION_FLOOR, OVERLAP_CAP,
)
from app.normalizers.reportall_owner import reportall_parcel_id, ReportAllOwnerNormalizer
from app.push.apg_adapter import ApgPushAdapter, FORBIDDEN_TABLES

BERRIEN = COUNTY_GEO["26021"]


def _situs(**kw):
    base = dict(address="73 W MAIN St", city="BENTON HARBOR", county="Berrien",
                state="MI", zip="49022")
    base.update(kw)
    return base


def _mail(**kw):
    base = dict(address="404 DUNE RIDGE S", city="SAINT JOSEPH", state="MI", zip="49085")
    base.update(kw)
    return base


# ---- owner_type ------------------------------------------------------------
def test_owner_type_individual():
    assert classify_owner_type("SMITH JOHN A") == "individual"
    assert classify_owner_type("PASCARELLA ANTHONY EDWARD") == "individual"


def test_owner_type_entity():
    assert classify_owner_type("ARP GLOBAL HOLDINGS LLC") == "entity"
    assert classify_owner_type("HARBERT WOODS ASSOCIATION") == "entity"
    assert classify_owner_type("FIRST BAPTIST CHURCH") == "entity"
    assert classify_owner_type("ACME PROPERTIES INC") == "entity"


def test_owner_type_govt():
    assert classify_owner_type("MICHIGAN STATE OF") == "govt"
    assert classify_owner_type("STATE OF MICHIGAN") == "govt"
    assert classify_owner_type("CITY OF BENTON HARBOR") == "govt"
    assert classify_owner_type("BERRIEN COUNTY LAND BANK") == "govt"
    assert classify_owner_type("LINCOLN TOWNSHIP") == "govt"


def test_owner_type_empty_defaults_individual():
    assert classify_owner_type(None) == "individual"
    assert classify_owner_type("") == "individual"


# ---- absentee_tier ---------------------------------------------------------
def test_tier_out_of_state():
    t = classify_absentee_tier(_situs(), _mail(state="IL", city="LOCKPORT", zip="60441"), BERRIEN)
    assert t == "out_of_state"


def test_tier_out_of_county_in_state():
    # Lansing (Ingham county) — in MI, not in Berrien
    t = classify_absentee_tier(_situs(), _mail(city="LANSING", state="MI", zip="48909",
                                               address="PO BOX 30448"), BERRIEN)
    assert t == "out_of_county"


def test_tier_out_of_town_same_county():
    # St Joseph is in Berrien but a different town than the Benton Harbor situs
    t = classify_absentee_tier(_situs(), _mail(city="SAINT JOSEPH", state="MI", zip="49085"),
                               BERRIEN)
    assert t == "out_of_town"


def test_tier_owner_occupied():
    s = _situs(address="100 ELM ST", zip="49022", city="BENTON HARBOR")
    m = _mail(address="100 ELM ST", city="BENTON HARBOR", state="MI", zip="49022")
    assert classify_absentee_tier(s, m, BERRIEN) == "owner_occupied"


def test_tier_vacant_lot_never_owner_occupied():
    # situs house number "0" (vacant) can't be owner-occupied even if streets match
    s = _situs(address="0 OAK DR", city="THREE OAKS", zip="49128")
    m = _mail(address="0 OAK DR", city="THREE OAKS", state="MI", zip="49128")
    assert classify_absentee_tier(s, m, BERRIEN) != "owner_occupied"


def test_tier_unknown_when_no_mailing():
    t = classify_absentee_tier(_situs(), {"address": None, "city": None, "state": None}, BERRIEN)
    assert t == "unknown"


# ---- absentee_score --------------------------------------------------------
def test_score_orders_by_tier():
    assert (absentee_score("out_of_state", "individual") >
            absentee_score("out_of_county", "individual") >
            absentee_score("out_of_town", "individual") >
            absentee_score("owner_occupied", "individual"))


def test_score_entity_premium():
    assert absentee_score("out_of_state", "entity") > absentee_score("out_of_state", "individual")
    assert absentee_score("out_of_state", "entity") == round(TIER_BASE["out_of_state"] * 1.10, 2)


def test_score_govt_floor():
    # an out_of_town govt parcel would be 50, but govt disposition floor lifts it
    assert absentee_score("out_of_town", "govt") == GOVT_DISPOSITION_FLOOR


def test_score_clamped_to_100():
    assert absentee_score("out_of_state", "entity") <= 100.0


def test_absentee_flag():
    assert absentee_flag("out_of_state") is True
    assert absentee_flag("out_of_town") is True
    assert absentee_flag("owner_occupied") is False
    assert absentee_flag("unknown") is False


# ---- signal_overlap --------------------------------------------------------
def test_signal_overlap_structure():
    ov = build_signal_overlap(["tax_delinquent", "environmental_part201", "weird_signal"],
                              {"name": "QOZ Tract 26021000400", "provenance_status": "authoritative"})
    assert ov["signal_count"] == 3
    assert ov["in_opportunity_zone"] is True
    assert ov["oz_tract"] == "QOZ Tract 26021000400"
    assert "tax_delinquent" in ov["distress_stack"]
    assert "environmental_part201" in ov["distress_stack"]
    assert "weird_signal" not in ov["distress_stack"]  # only known distress types count


def test_signal_overlap_empty():
    ov = build_signal_overlap([], None)
    assert ov["signal_count"] == 0
    assert ov["in_opportunity_zone"] is False
    assert ov["distress_stack"] == []


# ---- final_lead_score fusion ----------------------------------------------
def test_overlap_multiplier_components():
    # tax_delinquent (+0.20) + OZ (+0.15) = 1.35
    ov = build_signal_overlap(["tax_delinquent"], {"name": "x"})
    assert overlap_multiplier(ov) == 1.35


def test_overlap_multiplier_no_overlap_is_one():
    assert overlap_multiplier(build_signal_overlap([], None)) == 1.0


def test_overlap_multiplier_capped():
    ov = build_signal_overlap(
        ["tax_delinquent", "brownfield", "environmental_part201", "code_violation"],
        {"name": "x"})
    assert overlap_multiplier(ov) <= OVERLAP_CAP


def test_final_score_fusion_lifts_absentee():
    base = absentee_score("out_of_county", "individual")  # 70
    ov = build_signal_overlap(["tax_delinquent"], {"name": "x"})  # 1.35
    fused = final_lead_score(base, ov)
    assert fused > base
    assert fused == round(min(100.0, base * 1.35), 2)


def test_final_score_clamped_100():
    base = absentee_score("out_of_state", "entity")  # 99
    ov = build_signal_overlap(["tax_delinquent"], {"name": "x"})  # 1.35 -> 133.65
    assert final_lead_score(base, ov) == 100.0


def test_final_score_no_overlap_equals_absentee():
    base = absentee_score("out_of_state", "individual")
    assert final_lead_score(base, build_signal_overlap([], None)) == base


# ---- score_lead end-to-end -------------------------------------------------
def test_score_lead_top_decile_absentee_entity_distressed_oz():
    lead = dict(owner_name="ARP GLOBAL HOLDINGS LLC", situs_address="73 W MAIN St",
                situs_city="BENTON HARBOR", situs_county="Berrien", situs_state="MI",
                situs_zip="49022", owner_mailing_address="100 STATE ST",
                owner_mailing_city="CHICAGO", owner_mailing_state="IL", owner_mailing_zip="60601")
    res = score_lead(lead, ["tax_delinquent"],
                     {"name": "QOZ Tract X", "provenance_status": "authoritative"}, BERRIEN)
    assert res["owner_type"] == "entity"
    assert res["absentee_tier"] == "out_of_state"
    assert res["absentee_flag"] is True
    assert res["final_lead_score"] == 100.0  # 99 * 1.35 clamped


def test_score_lead_owner_occupied_low():
    lead = dict(owner_name="SMITH JOHN", situs_address="100 ELM ST", situs_city="BENTON HARBOR",
                situs_county="Berrien", situs_state="MI", situs_zip="49022",
                owner_mailing_address="100 ELM ST", owner_mailing_city="BENTON HARBOR",
                owner_mailing_state="MI", owner_mailing_zip="49022")
    res = score_lead(lead, [], None, BERRIEN)
    assert res["absentee_tier"] == "owner_occupied"
    assert res["absentee_flag"] is False
    assert res["final_lead_score"] == 20.0


# ---- homestead corroboration (annotation only; never moves the tier) -------
def test_score_lead_out_of_state_with_homestead_still_absentee():
    # A stale homestead exemption on an out-of-state owner does NOT make them an
    # occupant: the mailing/situs comparison is authoritative, so the tier stays
    # out_of_state, the lead is still flagged absentee, and the un-stripped exemption
    # is surfaced as stale_homestead -> still absentee, marginally stronger.
    lead = dict(owner_name="SMITH JOHN", situs_address="73 W MAIN St",
                situs_city="BENTON HARBOR", situs_county="Berrien", situs_state="MI",
                situs_zip="49022", owner_mailing_address="100 STATE ST",
                owner_mailing_city="CHICAGO", owner_mailing_state="IL", owner_mailing_zip="60601")
    res = score_lead(lead, [], None, BERRIEN, homestead_present=True)
    assert res["absentee_tier"] == "out_of_state"   # homestead did NOT change the tier
    assert res["absentee_flag"] is True             # still absentee
    assert res["stale_homestead"] is True           # stale exemption surfaced


def test_score_lead_homestead_does_not_force_owner_occupied_and_only_corroborates():
    # Genuinely owner-occupied (same address both sides). Homestead agrees with what the
    # address comparison already found, so it must NOT trip the "stale" flag (only off-
    # parcel exemptions are stale) and must NOT change the score.
    lead = dict(owner_name="SMITH JOHN", situs_address="100 ELM ST", situs_city="BENTON HARBOR",
                situs_county="Berrien", situs_state="MI", situs_zip="49022",
                owner_mailing_address="100 ELM ST", owner_mailing_city="BENTON HARBOR",
                owner_mailing_state="MI", owner_mailing_zip="49022")
    res = score_lead(lead, [], None, BERRIEN, homestead_present=True)
    assert res["absentee_tier"] == "owner_occupied"  # homestead can't flip it off-parcel...
    assert res["absentee_flag"] is False             # ...and can't make it absentee either
    assert res["stale_homestead"] is False           # corroborates only; not "stale"
    assert res["final_lead_score"] == 20.0           # score unchanged


def test_score_lead_homestead_is_annotation_only_not_a_score_change():
    # Off-parcel lead scored with vs without homestead: the ONLY difference is the
    # stale_homestead flag. Defaults to False (back-compat), and tier/scores are identical.
    lead = dict(owner_name="SMITH JOHN", situs_address="73 W MAIN St",
                situs_city="BENTON HARBOR", situs_county="Berrien", situs_state="MI",
                situs_zip="49022", owner_mailing_address="100 STATE ST",
                owner_mailing_city="CHICAGO", owner_mailing_state="IL", owner_mailing_zip="60601")
    without = score_lead(lead, [], None, BERRIEN)                          # param defaults False
    with_hs = score_lead(lead, [], None, BERRIEN, homestead_present=True)
    assert without["stale_homestead"] is False
    assert with_hs["stale_homestead"] is True
    assert without["absentee_tier"] == with_hs["absentee_tier"] == "out_of_state"
    assert without["absentee_score"] == with_hs["absentee_score"]
    assert without["final_lead_score"] == with_hs["final_lead_score"]
    assert without["raw_fused_score"] == with_hs["raw_fused_score"]


# ---- ReportAll APN mapping + normalize (real-data-only) --------------------
def test_reportall_parcel_id_strips_county_prefix():
    assert reportall_parcel_id("11-54-0340-0145-00-9", "11-") == "54-0340-0145-00-9"


def test_reportall_parcel_id_prefixless_as_is():
    assert reportall_parcel_id("07-5620-0075-00-8", "11-") == "07-5620-0075-00-8"


def _raw_result():
    return {
        "parcel_id": "54-0340-0145-00-9", "alt_id_2": "11-54-0340-0145-00-9",
        "address": "73 W MAIN St", "addr_city": "BENTON HARBOR", "county_name": "Berrien",
        "state_abbr": "MI", "addr_zip": "49022", "latitude": "42.116", "longitude": "-86.455",
        "owner": "ARP GLOBAL HOLDINGS LLC", "mail_address1": "404 DUNE RIDGE S",
        "mail_placename": "SAINT JOSEPH", "mail_statename": "MI", "mail_zipcode": "49085",
        "land_use_class": "Commercial", "land_use_code": "202",
        "acreage": "0.0000", "acreage_calc": "0.06", "robust_id": "AAB",
    }


def test_normalize_maps_real_fields_and_keeps_csv_apn():
    norm = ReportAllOwnerNormalizer("t", "26021", ["11-54-0340-0145-00-9"], client_key="x")
    leads = norm.normalize([("11-54-0340-0145-00-9", _raw_result())])
    assert len(leads) == 1
    d = leads[0]
    assert d["parcel_apn"] == "11-54-0340-0145-00-9"  # canonical CSV form preserved
    assert d["owner_name"] == "ARP GLOBAL HOLDINGS LLC"
    assert d["owner_mailing_city"] == "SAINT JOSEPH"
    assert d["owner_mailing_state"] == "MI"
    assert d["acreage"] == 0.06  # falls back to acreage_calc when acreage is 0
    assert d["raw_meta"]["alt_id_2"] == "11-54-0340-0145-00-9"
    assert d["raw_meta"]["situs_county"] == "Berrien"


def test_normalize_skips_misses_no_fabrication():
    norm = ReportAllOwnerNormalizer("t", "26021", ["X", "Y"], client_key="x")
    leads = norm.normalize([("X", None), ("Y", _raw_result())])
    assert len(leads) == 1  # the None (count=0 miss) is dropped, never invented
    assert leads[0]["owner_name"] == "ARP GLOBAL HOLDINGS LLC"


def test_normalizer_rejects_unknown_county():
    import pytest
    with pytest.raises(ValueError):
        ReportAllOwnerNormalizer("t", "99999", ["a"], client_key="x")


def test_mailing_csz_parser_fallback():
    norm = ReportAllOwnerNormalizer("t", "26021", ["a"], client_key="x")
    r = _raw_result()
    del r["mail_placename"]; del r["mail_statename"]; del r["mail_zipcode"]
    r["mail_address3"] = "BENTON HARBOR MI 49022"
    leads = norm.normalize([("a", r)])
    assert leads[0]["owner_mailing_state"] == "MI"
    assert leads[0]["owner_mailing_zip"] == "49022"


# ---- CSV export helpers ----------------------------------------------------
def test_csv_helpers():
    from scripts.owner_leads import _mailing_str, _overlap_str
    lead = dict(owner_mailing_address="404 DUNE RIDGE S", owner_mailing_city="SAINT JOSEPH",
                owner_mailing_state="MI", owner_mailing_zip="49085")
    assert _mailing_str(lead) == "404 DUNE RIDGE S, SAINT JOSEPH MI 49085"
    ov = build_signal_overlap(["tax_delinquent"], {"name": "QOZ Tract X"})
    s = _overlap_str(ov)
    assert "tax_delinquent" in s and "OZ" in s
    assert _overlap_str(build_signal_overlap([], None)) == "none"


def test_county_alias_resolution():
    from scripts.owner_leads import county_alias_to_fips
    assert county_alias_to_fips("berrien") == "26021"
    assert county_alias_to_fips("26021") == "26021"
    assert county_alias_to_fips("nowhere") is None


# ---- GATED skip-trace stub (Tracerfy) --------------------------------------
def test_tracerfy_cost_math_and_disabled(capsys):
    # the disabled path: compute cost, print the awaiting-go message, call nothing
    from scripts.export_outreach import TRACERFY_COST_PER_LEAD
    n = 137
    cost = n * TRACERFY_COST_PER_LEAD
    print(f"TRACERFY DISABLED — would trace {n} leads at "
          f"${TRACERFY_COST_PER_LEAD:.2f} = ${cost:.2f}, awaiting go")
    out = capsys.readouterr().out
    assert "TRACERFY DISABLED" in out
    assert "$2.74" in out  # 137 * 0.02
    assert TRACERFY_COST_PER_LEAD == 0.02


def test_tracerfy_call_requires_explicit_function():
    # the real call lives in a named function — it is NOT wired into any default path
    import scripts.export_outreach as eo
    assert hasattr(eo, "tracerfy_batch_trace")


# ---- GATED APG push stub ---------------------------------------------------
def _lead_row():
    return dict(lead_id="abc", county_fips="26021", parcel_apn="11-54-0340-0145-00-9",
                owner_name="ARP GLOBAL HOLDINGS LLC", owner_type="entity",
                owner_mailing_address="404 DUNE RIDGE S", owner_mailing_city="SAINT JOSEPH",
                owner_mailing_state="MI", owner_mailing_zip="49085",
                situs_address="73 W MAIN St", land_use_class="Commercial", acreage=0.06,
                absentee_tier="out_of_state", absentee_score=99.0, final_lead_score=100.0,
                signal_overlap={"distress_stack": ["tax_delinquent"]})


def test_apg_push_gated_off_by_default(capsys):
    adapter = ApgPushAdapter()
    res = adapter.push([_lead_row()], do_push=False)
    assert res.gated is True
    assert res.pushed == 0
    out = capsys.readouterr().out
    assert "APG PUSH DISABLED" in out
    assert "builder_buyers" in out  # message names the table NOT to use


def test_apg_push_destination_unconfirmed_raises_even_if_forced():
    import pytest
    adapter = ApgPushAdapter()  # dest_table defaults to None (unconfirmed)
    with pytest.raises(RuntimeError, match="UNCONFIRMED"):
        adapter.push([_lead_row()], do_push=True)


def test_apg_builder_buyers_hard_blocked():
    import pytest
    assert "builder_buyers" in FORBIDDEN_TABLES
    adapter = ApgPushAdapter(dest_table="builder_buyers")
    with pytest.raises(RuntimeError, match="BUYERS"):
        adapter.push([_lead_row()], do_push=True)


def test_apg_mapping_has_no_buyer_fields():
    # owner leads are SUPPLY — the mapped row must not masquerade as buyer demand
    row = ApgPushAdapter().map_lead(_lead_row())
    assert row["source_engine"] == "landos:owner_outreach"
    assert row["owner_name"] == "ARP GLOBAL HOLDINGS LLC"
    forbidden = {"buyer_name", "buyer_id", "demand", "builder_id", "buy_box"}
    assert not (forbidden & set(row.keys()))


def test_apg_connect_blocked_without_url(monkeypatch):
    import pytest
    monkeypatch.delenv("APG_DATABASE_URL", raising=False)
    adapter = ApgPushAdapter(dest_table="some_confirmed_table")
    # destination passes validation now, but connection must still refuse without creds
    with pytest.raises(RuntimeError, match="APG_DATABASE_URL"):
        adapter.push([_lead_row()], do_push=True)


# ---- v1.1.0 de-compressed ranking -----------------------------------------
def test_raw_fused_score_unclamped_separates_stacks():
    # the clamp hid this: ONE signal vs THREE both clamp to 100, but raw separates
    abs99 = 99.0
    one = build_signal_overlap(["environmental_part201"], None)              # x1.10
    three = build_signal_overlap(["tax_delinquent", "brownfield", "environmental_part201"],
                                 {"name": "x"})                              # x1.55
    assert final_lead_score(abs99, one) == final_lead_score(abs99, three) == 100.0  # compressed
    assert raw_fused_score(abs99, three) > raw_fused_score(abs99, one)              # de-compressed
    assert raw_fused_score(abs99, three) > 100.0  # unclamped can exceed 100


def test_raw_fused_score_values():
    # 99 * (1 + 0.20 tax + 0.15 OZ) = 99 * 1.35 = 133.65
    ov = build_signal_overlap(["tax_delinquent"], {"name": "x"})
    assert raw_fused_score(99.0, ov) == 133.65


def test_tiebreak_distress_count():
    hi = build_signal_overlap(["tax_delinquent", "brownfield"], None)
    lo = build_signal_overlap(["tax_delinquent"], None)
    # same raw forced -> more distinct distress signals wins
    assert tiebreak_key(100.0, hi, "out_of_state", 1.0, "entity") > \
           tiebreak_key(100.0, lo, "out_of_state", 1.0, "entity")


def test_tiebreak_tax_beats_passive():
    tax = build_signal_overlap(["tax_delinquent"], None)
    env = build_signal_overlap(["environmental_part201"], None)
    assert tiebreak_key(100.0, tax, "out_of_state", 1.0, "entity") > \
           tiebreak_key(100.0, env, "out_of_state", 1.0, "entity")


def test_tiebreak_tier_then_acreage_then_individual():
    ov = build_signal_overlap(["tax_delinquent"], None)
    # tier: out_of_state > out_of_county
    assert tiebreak_key(100.0, ov, "out_of_state", 1.0, "entity") > \
           tiebreak_key(100.0, ov, "out_of_county", 1.0, "entity")
    # acreage: bigger lot wins when all else equal
    assert tiebreak_key(100.0, ov, "out_of_state", 10.0, "entity") > \
           tiebreak_key(100.0, ov, "out_of_state", 1.0, "entity")
    # individual over entity for cold-outreach when all else equal
    assert tiebreak_key(100.0, ov, "out_of_state", 1.0, "individual") > \
           tiebreak_key(100.0, ov, "out_of_state", 1.0, "entity")


def _norm_top(results):
    """Mirror the script's batch normalization: raw -> 0..100 against the max."""
    raws = [r["raw_fused_score"] for r in results]
    mx = max(raws) or 1.0
    return sorted((round(r / mx * 100.0, 2) for r in raws), reverse=True)


def test_decompression_top10_not_all_identical():
    # a realistic mix of profiles must NOT collapse to a wall of 100s
    geo = COUNTY_GEO["26021"]
    def mk(owner, st, overlap_signals, oz):
        lead = dict(owner_name=owner, situs_address="0 X St", situs_city="BENTON HARBOR",
                    situs_county="Berrien", situs_state="MI", situs_zip="49022",
                    owner_mailing_address="1 FAR ST", owner_mailing_city="DENVER",
                    owner_mailing_state=st, owner_mailing_zip="80202")
        return score_lead(lead, overlap_signals, {"name": "OZ"} if oz else None, geo)
    results = [
        mk("A LLC", "CO", ["tax_delinquent", "brownfield", "environmental_part201"], True),
        mk("B LLC", "CO", ["tax_delinquent"], True),
        mk("C LLC", "CO", ["environmental_part201"], True),
        mk("D LLC", "CO", ["environmental_part201"], False),
        mk("E SMITH", "CO", ["tax_delinquent"], False),
        mk("F LLC", "CO", [], False),
    ]
    top = _norm_top(results)
    assert top[0] == 100.0                      # top normalizes to the ceiling
    assert len(set(top)) > 1                    # NOT all identical — de-compressed
    assert len(set(top[:5])) >= 4               # genuinely spread across the range


# ---- St. Joseph IN (18141): ingest-id vs match-id split + situs degrade --------
def test_in_county_config_present():
    from app.normalizers.reportall_owner import COUNTY_CONFIG
    cfg = COUNTY_CONFIG["18141"]
    assert cfg["region"] == "St. Joseph County, IN"
    assert cfg["state_abbr"] == "IN"
    assert cfg["id_mode"] == "reportall_native"
    assert "strip_prefix" not in cfg          # IN must NOT reuse the Berrien strip model
    assert "18141" in COUNTY_GEO               # county-membership reference exists


def _in_record(**over):
    r = {
        "parcel_id": "018-8034-1460",                 # ReportAll internal ingest id
        "alt_id_1": "71-08-14-405-006.000-026",        # human county APN (the match key)
        "alt_id_2": None,
        "owner": "SMITH MELISSA A",
        "mail_placename": "SOUTH BEND", "mail_statename": "IN", "mail_zipcode": "46613",
        "mail_address1": "1720 Kemble",
        "address": "1720 KEMBLE Ave", "addr_city": "SOUTH BEND", "county_name": "St. Joseph",
        "state_abbr": "IN", "addr_zip": "46613", "latitude": "41.66", "longitude": "-86.25",
        "land_use_class": "Residential", "acreage": "0.1500",
    }
    r.update(over)
    return r


def test_in_normalize_parcel_apn_from_alt_id_1():
    from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
    norm = ReportAllOwnerNormalizer("t", "18141", client_key="x")
    leads = norm.normalize([("018-8034-1460", _in_record())])
    assert len(leads) == 1
    d = leads[0]
    assert d["parcel_apn"] == "71-08-14-405-006.000-026"   # match key = alt_id_1 (71-form)
    assert d["reportall_parcel_id"] == "018-8034-1460"      # ingest id = parcel_id (018-form)
    assert d["raw_meta"]["alt_id_1"] == "71-08-14-405-006.000-026"
    assert d["owner_name"] == "SMITH MELISSA A"
    assert d["owner_mailing_state"] == "IN"


def test_in_normalize_skips_record_missing_alt_id_1():
    from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
    norm = ReportAllOwnerNormalizer("t", "18141", client_key="x")
    leads = norm.normalize([("018-0000-0000", _in_record(alt_id_1=None))])
    assert leads == []          # no county APN -> no match key -> skip, never fabricate


def test_tier_unknown_situs_when_no_situs_street():
    # in-county mailing (South Bend, in St. Joseph) but situs street unknown ->
    # cannot tell owner_occupied from out_of_town -> unknown_situs (not owner_occupied)
    geo = COUNTY_GEO["18141"]
    situs = {"address": None, "city": "SOUTH BEND", "county": "St. Joseph", "state": "IN", "zip": None}
    mailing = {"address": "1720 Kemble", "city": "SOUTH BEND", "state": "IN", "zip": "46613"}
    t = classify_absentee_tier(situs, mailing, geo)
    assert t == "unknown_situs"
    assert absentee_flag(t) is False           # we do NOT claim it's absentee


def test_tier_out_of_state_resolves_without_situs_street():
    # situs street null, but mailing in a different state -> still out_of_state
    geo = COUNTY_GEO["18141"]
    situs = {"address": None, "city": "SOUTH BEND", "county": "St. Joseph", "state": "IN", "zip": None}
    mailing = {"address": "1 FAR ST", "city": "CHICAGO", "state": "IL", "zip": "60601"}
    assert classify_absentee_tier(situs, mailing, geo) == "out_of_state"


def test_tier_out_of_county_resolves_without_situs_street():
    # situs street null, mailing in-state IN but not in St. Joseph -> out_of_county
    geo = COUNTY_GEO["18141"]
    situs = {"address": None, "city": "SOUTH BEND", "county": "St. Joseph", "state": "IN", "zip": None}
    mailing = {"address": "5 N ST", "city": "INDIANAPOLIS", "state": "IN", "zip": "46204"}
    assert classify_absentee_tier(situs, mailing, geo) == "out_of_county"
