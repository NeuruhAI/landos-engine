"""Read-only connections for free-first research, each from its one authority.

  spine   the DeedSonar contact graph (T6 / I2 / I3 records), resolved ONLY through
          app.contact_authority.resolve_contact_dsn -- the single contact-DB authority
          (the legacy spine is refused; the engine's own DSN is never used for it).
  engine  the engine's own database (landos.owner_leads, the local_cache tier), from
          app.config -- the engine's configuration.

Both sessions are set read_only before any query. A source that is absent or refused is
simply not returned (with its refusal code): its steps stay UNWIRED and paid stays blocked.
This module names no contact-graph table; route_executors receives the connections.
"""
from __future__ import annotations

from typing import Any, Dict


def open_research_connections(*, spine: bool = True, engine: bool = True) -> Dict[str, Any]:
    import psycopg
    out: Dict[str, Any] = {}
    if spine:
        from app.contact_authority import ContactAuthorityError, resolve_contact_dsn
        try:
            authority = resolve_contact_dsn(required=False)
            if authority is None:
                out["spine_refused"] = "CONTACT_DSN_MISSING"
            else:
                conn = psycopg.connect(authority.dsn, connect_timeout=15)
                conn.read_only = True
                out["spine"], out["spine_authority"] = conn, authority.describe()
        except ContactAuthorityError as exc:
            out["spine_refused"] = exc.code
        except psycopg.Error as exc:
            out["spine_refused"] = type(exc).__name__
    if engine:
        try:
            from app.config import DATABASE_URL as ENGINE_DSN
            conn = psycopg.connect(ENGINE_DSN, connect_timeout=15)
            conn.read_only = True
            out["engine"] = conn
        except (psycopg.Error, RuntimeError) as exc:
            out["engine_refused"] = type(exc).__name__
    return out


def close_all(conns: Dict[str, Any]) -> None:
    for key in ("spine", "engine"):
        conn = conns.get(key)
        if conn is not None:
            conn.close()
