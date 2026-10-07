"""auth.py — pure, dependency-free credential checks for the engine's token-guarded routes.

FAIL-CLOSED doctrine: a protected route is reachable only when a NON-EMPTY presented credential
matches a NON-EMPTY configured secret. An unset token is a LOCKED door, never an open one — the
same doctrine as DNC-unknown = blocked: absence of proof is denial, not permission. "Read-only"
(county-public valuation data) is NOT "unauthenticated"; an unset NVE_LAND_TOKEN must 401, not
serve the world.

This module imports nothing heavy (no db, no pool, no config) so the guard logic is unit-testable
without a database. main.py's auth_land_dd / auth_hunt / auth_composer all funnel through
token_guard_ok, so the fail-closed behaviour is defined in exactly one tested place.
"""
from __future__ import annotations


def bearer_of(authorization: str) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header, else ''."""
    a = authorization or ""
    return a[7:].strip() if a.lower().startswith("bearer ") else ""


def token_guard_ok(bearer: str, x_landos_secret: str, token: str, landos_secret: str) -> bool:
    """True iff a non-empty presented credential matches a non-empty configured secret.

    Two accepted credentials:
      - Bearer path: enabled ONLY when `token` (NVE_LAND_TOKEN / COMPOSER_TOKEN) is configured;
        the presented bearer must equal it.
      - Secret path: the engine's master X-LANDOS-SECRET is always an accepted credential.

    Every comparison requires BOTH sides non-empty, so an unset token (or unset secret) can never
    grant access via an empty == empty match. No configured credential + no valid presented
    credential => False => the caller 401s. Fail-closed, unconditionally.
    """
    if token and bearer and bearer == token:
        return True
    if landos_secret and x_landos_secret and x_landos_secret == landos_secret:
        return True
    return False
