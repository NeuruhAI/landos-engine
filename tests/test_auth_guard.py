"""test_auth_guard.py — the fail-closed doctrine for every token-guarded route, as a test.

Pure, no DB / no network (imports app.auth only, never app.main which opens the pool). Locks the
one bug this fix closes: /land-dd (and its 5 siblings) served UNAUTHENTICATED whenever
NVE_LAND_TOKEN was unset. An unset token is now a LOCKED door — 401 — never open.

Required cases from the mission: unset -> 401, wrong -> 401, correct -> 200. Here "-> 401" is
token_guard_ok(...) is False (the guard raises 401) and "-> 200" is True (the dependency passes
and the route runs). auth_land_dd / auth_hunt / auth_composer all funnel through token_guard_ok,
so this one test covers all three.
"""
from app.auth import bearer_of, token_guard_ok

SECRET = "master-landos-secret"
TOKEN = "nve-land-token-abc123"


# ── bearer_of ────────────────────────────────────────────────────────────────
def test_bearer_extraction():
    assert bearer_of("Bearer abc123") == "abc123"
    assert bearer_of("bearer abc123") == "abc123"          # case-insensitive scheme
    assert bearer_of("Bearer   spaced  ") == "spaced"
    assert bearer_of("") == ""
    assert bearer_of("Basic abc123") == ""                 # wrong scheme -> no bearer
    assert bearer_of("abc123") == ""                        # no scheme -> no bearer


# ── THE HOLE THAT SHIPPED: unset token must NEVER be open ─────────────────────
def test_unset_token_no_secret_is_locked():
    # NVE_LAND_TOKEN unset (token="") AND no presented secret -> DENIED. This is the exact
    # regression: before the fix this returned True (open). It must be False (401) forever.
    assert token_guard_ok(bearer="", x_landos_secret="", token="", landos_secret=SECRET) is False


def test_unset_token_ignores_any_bearer():
    # With no token configured, NO bearer value can authenticate via the bearer path.
    assert token_guard_ok(bearer="anything", x_landos_secret="", token="", landos_secret=SECRET) is False
    assert token_guard_ok(bearer=SECRET, x_landos_secret="", token="", landos_secret=SECRET) is False


# ── wrong -> 401 ──────────────────────────────────────────────────────────────
def test_wrong_bearer_denied():
    assert token_guard_ok(bearer="wrong", x_landos_secret="", token=TOKEN, landos_secret=SECRET) is False


def test_wrong_secret_denied():
    assert token_guard_ok(bearer="", x_landos_secret="nope", token=TOKEN, landos_secret=SECRET) is False


# ── correct -> 200 ────────────────────────────────────────────────────────────
def test_correct_bearer_allowed():
    assert token_guard_ok(bearer=TOKEN, x_landos_secret="", token=TOKEN, landos_secret=SECRET) is True


def test_master_secret_always_allowed():
    # The engine's X-LANDOS-SECRET is a valid credential even when no bearer token is configured.
    assert token_guard_ok(bearer="", x_landos_secret=SECRET, token="", landos_secret=SECRET) is True
    assert token_guard_ok(bearer="", x_landos_secret=SECRET, token=TOKEN, landos_secret=SECRET) is True


# ── empty-vs-empty must never match (defense against unset SECRET too) ─────────
def test_empty_secret_config_cannot_be_matched():
    # If LANDOS_SECRET itself were empty, an empty presented secret must still be denied.
    assert token_guard_ok(bearer="", x_landos_secret="", token="", landos_secret="") is False
    assert token_guard_ok(bearer="", x_landos_secret="", token="", landos_secret=None or "") is False
