"""
test_parcel_owner_lane.py — JOB 5.6: landos.parcel_owner load + the owner-joined
plat RPC (migrations 022/023). Pure: the govt vintage-flag pattern, mailing
assembly, and SQL invariants. The live acceptance (LaPorte APN -> subject + 4
neighbors with owner names, govt-flagged school corp, unknown -> Error,
grants f/f/t, 63,979 owners / 4,094 govt) ran at apply time, pinned in the
commit receipt.
"""
import os
import re

from scripts.load_parcel_owner import (assemble_mailing, vintage_flag_for,
                                        COUNTIES, INSERT_SQL)

MIG_TABLE = os.path.join(os.path.dirname(__file__), "..", "migrations", "022_parcel_owner.sql")
MIG_FUNC = os.path.join(os.path.dirname(__file__), "..", "migrations",
                        "023_fn_parcel_plat_owner_join.sql")


def _read(p):
    with open(p) as f:
        return f.read()


def _flat(p):
    return re.sub(r"\s+", " ", _read(p).lower())


# ---- vintage-capture law (the govt pattern) ----------------------------------------

def test_govt_pattern_flags_public_entities():
    for name in ("City Of Michigan City", "Michigan City School Building Corporation",
                 "Laporte County Treasurer", "Board Of Aviation Of Laporte County",
                 "State Of Indiana", "Housing Authority Of South Bend",
                 "Kankakee Township", "Michigan City Sanitary District"):
        assert vintage_flag_for(name) == "govt", name


def test_govt_pattern_does_not_trip_on_surnames():
    # word-boundaried: these private owners must stay unflagged (null)
    for name in ("Burns John W Trustee of the Burns Family Rev Trust",
                 "Townsend Mary", "Parker Robert L", "Forestier Anne",
                 "Nondorf Eunice Oconnor Rev Trust", "Budka Mark A & Tracy L Reilly"):
        assert vintage_flag_for(name) is None, name


def test_blank_name_is_not_govt():
    assert vintage_flag_for("") is None and vintage_flag_for(None) is None


# ---- mailing assembly --------------------------------------------------------------

def test_mailing_assembles_and_skips_blanks():
    row = {"owner_mailing_address": "123 Main St", "owner_mailing_city": "La Porte",
           "owner_mailing_state": "IN", "owner_mailing_zip": "46350"}
    assert assemble_mailing(row) == "123 Main St, La Porte IN 46350"
    assert assemble_mailing({"owner_mailing_address": "PO BOX 5"}) == "PO BOX 5"
    assert assemble_mailing({}) is None


# ---- table migration (022) ---------------------------------------------------------

def test_table_is_additive_pk_rls_and_vintage_flag():
    s = _flat(MIG_TABLE)
    assert "create table if not exists landos.parcel_owner" in s
    assert "primary key (county_fips, parcel_apn)" in s     # the (county,apn) index
    assert "vintage_flag  text" in s or "vintage_flag text" in s
    assert "enable row level security" in s
    for banned in ("drop table", "delete from", "truncate"):
        assert banned not in s


def test_loader_upsert_is_on_conflict_do_nothing():
    s = re.sub(r"\s+", " ", INSERT_SQL.lower())
    assert "insert into landos.parcel_owner" in s
    assert "on conflict (county_fips, parcel_apn) do nothing" in s
    assert COUNTIES["laporte"]["county_fips"] == "18091"


# ---- function migration (023) ------------------------------------------------------

def test_func_joins_parcel_owner_not_owner_leads():
    s = _flat(MIG_FUNC)
    assert "left join landos.parcel_owner po" in s
    assert "on ol." not in s                                # owner_leads seam is gone
    assert "po.owner_name" in s and "po.vintage_flag" in s


def test_func_owner_flag_reads_stored_vintage_and_is_honest_null():
    s = _flat(MIG_FUNC)
    assert "when n.vintage_flag = 'govt' then 'govt'" in s   # stored flag, not re-derived
    assert "then 'not_on_file'" in s                         # honest-null for absent owner
    assert "'not on file'" in s                              # displayed owner string
    assert "'parcel_not_found'" in s                         # unknown APN -> Error, no fabrication


def test_func_postgis_on_search_path_and_service_role_only():
    s = _flat(MIG_FUNC)
    assert "search_path = landos, public, extensions, pg_temp" in s   # PostGIS in extensions
    assert "grant execute on function landos.fn_parcel_plat(text, text) to service_role" in s
    assert "from anon" in s and "from authenticated" in s
    assert "security invoker" in s
