"""
test_fn_parcel_plat.py — invariants of migrations/021_fn_parcel_plat.sql
(the sanctioned CRM read path for parcel geometry). No DB: the migration text
is the contract; the live acceptance (LaPorte subject + >=1 neighbor,
honest-empty miss, grant matrix f/f/t) ran at apply time and is pinned in the
commit receipt.
"""
import os
import re

MIG = os.path.join(os.path.dirname(__file__), "..",
                   "migrations", "021_fn_parcel_plat.sql")


def _sql():
    with open(MIG) as f:
        return f.read()


def _flat():
    return re.sub(r"\s+", " ", _sql().lower())


def test_signature_and_return_shape():
    s = _flat()
    assert "create or replace function landos.fn_parcel_plat(p_county text, p_apn text)" in s
    assert "returns jsonb" in s and "stable" in s
    assert "'featurecollection'" in s
    assert "coalesce(jsonb_agg(f), '[]'::jsonb)" in s     # honest-empty, never NULL/error


def test_neighbors_are_touching_same_county_not_self():
    s = _flat()
    assert "st_touches(n.geom, s.geom)" in s
    assert "n.geom && s.geom" in s                        # GIST prefilter
    assert "n.county_fips = s.county_fips" in s
    assert "n.parcel_apn <> s.parcel_apn" in s
    assert "'subject'" in s and "'neighbor'" in s          # roles labeled per feature


def test_execute_is_service_role_only_and_invoker():
    s = _flat()
    assert "revoke all on function landos.fn_parcel_plat(text, text) from public" in s
    assert "revoke all on function landos.fn_parcel_plat(text, text) from anon" in s
    assert "revoke all on function landos.fn_parcel_plat(text, text) from authenticated" in s
    assert "grant execute on function landos.fn_parcel_plat(text, text) to service_role" in s
    assert "security definer" not in s                    # invoker on purpose (RLS + bypassrls)


def test_additive_only():
    s = _flat()
    for banned in ("drop table", "drop function", "delete from", "truncate", "alter table"):
        assert banned not in s
