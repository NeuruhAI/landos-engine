"""
test_field_pins.py — SIGNAL-IQ FIELD P3: the field_pins table + ray resolver
(migrations 024/025). Pure SQL-text invariants; the live resolution ladder ran
at apply time (rollback-smokes pinned in the commit receipt):
  ray_intersect -> real LaPorte APN + owner + geometry, conf 0.90;
  dark county (Berrien) -> method 'none', coverage 'dark_county', geometry null;
  unknown pin -> {ok:false, error:'pin_not_found'}.
"""
import os
import re

TBL = os.path.join(os.path.dirname(__file__), "..", "migrations", "024_field_pins.sql")
FN = os.path.join(os.path.dirname(__file__), "..", "migrations", "025_fn_resolve_field_pin.sql")


def _flat(p):
    with open(p) as f:
        return re.sub(r"\s+", " ", f.read().lower())


# ---- table (024) -------------------------------------------------------------------

def test_table_shape_and_honesty_columns():
    s = _flat(TBL)   # whitespace collapsed to single spaces
    assert "create table if not exists landos.field_pins" in s
    assert "origin geometry(point, 4326) not null" in s
    assert "ray geometry(linestring, 4326)" in s
    # the two never-drop honesty columns + their domains
    assert "heading_source" in s and "check (heading_source in ('gps_course','fix_delta','compass','unknown'))" in s
    assert "resolution_method" in s and "check (resolution_method in ('ray_intersect','nearest_parcel','none'))" in s
    assert "side text not null check (side in ('left','right','ahead','at','unknown'))" in s


def test_table_is_additive_indexed_and_rls():
    s = _flat(TBL)
    assert "using gist (origin)" in s and "using gist (ray)" in s
    assert "where resolved_apn is null" in s          # the unresolved-pin partial index
    assert "enable row level security" in s
    for banned in ("drop table", "delete from", "truncate"):
        assert banned not in s


# ---- resolver (025) ----------------------------------------------------------------

def test_resolver_ladder_confidences_and_buffer():
    s = _flat(FN)
    assert "st_intersects(g.geom, pin.ray)" in s
    assert "st_distance(g.geom::geography, pin.origin::geography) > 3" in s   # 3 m ROW buffer
    assert "'ray_intersect'" in s and "conf := 0.90" in s
    assert "'nearest_parcel'" in s and "conf := 0.50" in s
    assert "st_dwithin(g.geom::geography, pin.projected::geography, 60)" in s  # 60 m nearest cap
    assert "'none'" in s and "'dark_county'" in s     # honest terminal state


def test_resolver_nearest_only_when_unknown_or_ray_missed():
    s = _flat(FN)
    assert "pin.side = 'unknown' or pin.ray is null or method is null" in s


def test_resolver_owner_join_honors_vintage_law():
    s = _flat(FN)
    assert "from landos.parcel_owner" in s
    assert "owner_flag := 'govt'" in s                 # govt flagged, never a seller
    assert "owner_flag := 'not_on_file'" in s          # honest-null
    assert "'not on file'" in s


def test_resolver_checks_the_book_and_is_service_role_only():
    s = _flat(FN)
    assert "from public.acquisition_leads" in s and "'in_crm'" in s
    assert "'pin_not_found'" in s                       # honest miss, not an empty state
    assert "security definer" in s
    assert "grant  execute on function landos.fn_resolve_field_pin(uuid) to service_role".replace("  ", " ") in s.replace("  ", " ")
    assert "from anon" in s and "from authenticated" in s


def test_resolver_search_path_includes_postgis_extensions():
    s = _flat(FN)
    assert "search_path = landos, public, extensions, pg_temp" in s
