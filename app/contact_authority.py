"""
contact_authority.py — the ONE database the DeedSonar contact graph lives in.

Why this file exists: the contact projection, the next-best-source refresh, the
operator importer, the free-route (NCSBE) attempt writer and the identity
projections all read or write DeedSonar's `public` contact graph
(candidate_contact_resolution, parties, candidate_parties, contact_evidence,
party_phones, free_route_attempts, entity_identity, tier_c_adjudication). Each
used to take whatever `DATABASE_URL` held. On this host that is the legacy spine
`lvdimujkfheeyqchjzra`, retired as DeedSonar's database at the 2026-09-08 Pro
cutover. So every run kept projecting into a database production no longer reads,
and nothing said so.

This module resolves the contact DSN in ONE place and refuses the wrong one:

  * Source order: an explicit `--database-url`, then `DEEDSONAR_DATABASE_URL`, then
    `DATABASE_URL` (the order `project_contacts_to_deedsonar.py` already used).
    The engine's own `DATABASE_URL` is NOT changed by this module; the engine's
    `landos` schema and `sync_to_crm` keep it until the engine DB-authority
    ruling (T0).
  * The retired legacy ref is refused wherever it appears (DSN, PGHOST, PGUSER,
    PGOPTIONS). There is no override.
  * Any other Supabase project is refused: DeedSonar has one production database,
    `efxowqudmsfqjmbeidjc`.
  * A loopback / unix-socket database (a replica court, a test cluster) is
    allowed and is labelled NON_PRODUCTION.
  * Any other host (an IP literal, a proxy, an alias whose project cannot be
    read from the DSN) is refused unless CONTACT_DB_ALLOW_NONCANONICAL=1, because
    a string check cannot prove where it lands.

Every tool prints `authority.describe()` (never the password) before it connects.
Importing this module does not import psycopg, so the check costs nothing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit

CANONICAL_REF = "efxowqudmsfqjmbeidjc"
RETIRED_REFS = {
    "lvdimujkfheeyqchjzra": "legacy spine, retired as DeedSonar's DB at the 2026-09-08 Pro cutover",
}
ENV_VAR = "DEEDSONAR_DATABASE_URL"
FALLBACK_ENV_VAR = "DATABASE_URL"
ALLOW_NONCANONICAL_VAR = "CONTACT_DB_ALLOW_NONCANONICAL"

CANONICAL = "CANONICAL"
NON_PRODUCTION = "NON_PRODUCTION"
NONCANONICAL_ALLOWED = "NONCANONICAL_ALLOWED"

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_REF_RE = re.compile(r"^[a-z0-9]{20}$")


class ContactAuthorityError(RuntimeError):
    """The contact DSN is missing or is not the contact authority. Exit, never write."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ContactAuthority:
    dsn: str
    source: str             # "--database-url" | DEEDSONAR_DATABASE_URL | DATABASE_URL
    klass: str              # CANONICAL | NON_PRODUCTION | NONCANONICAL_ALLOWED
    ref: Optional[str]      # Supabase project ref when the DSN names one
    host: str

    def describe(self) -> str:
        return (f"CONTACT_DB_AUTHORITY={self.ref or self.host or 'local-socket'} "
                f"class={self.klass} source={self.source}")


def _conninfo_pairs(dsn: str) -> dict:
    """Parse libpq key=value conninfo (the non-URI form). Quoted values allowed."""
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn):
        v = m.group(2)
        if v.startswith("'") and v.endswith("'"):
            v = v[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        out[m.group(1).lower()] = v
    return out


def _host_user(dsn: str, env: Mapping[str, str]) -> tuple[str, str]:
    if dsn.startswith(("postgres://", "postgresql://")):
        parts = urlsplit(dsn)
        host = unquote(parts.hostname or "")
        user = unquote(parts.username or "")
        if not host:  # postgresql:///db?host=/tmp — host in the query string
            q = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
            host = unquote(q.get("host", ""))
            user = user or unquote(q.get("user", ""))
    else:
        kv = _conninfo_pairs(dsn)
        host = kv.get("host") or kv.get("hostaddr") or ""
        user = kv.get("user") or ""
    return (host or env.get("PGHOST", "")).strip().lower(), (user or env.get("PGUSER", "")).strip()


def supabase_ref(host: str, user: str) -> Optional[str]:
    """The project ref from a direct host (db.<ref>.supabase.co) or a pooler user (postgres.<ref>)."""
    if host.endswith((".supabase.co", ".supabase.com")):
        for label in host.split("."):
            if _REF_RE.match(label):
                return label
    if "." in user:
        tail = user.rsplit(".", 1)[1].lower()
        if _REF_RE.match(tail):
            return tail
    return None


def _load_repo_dotenv() -> None:
    """Same file app/config.py loads: the engine's .env. Existing env vars win."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # the tools run fine on exported env alone
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def classify(dsn: str, source: str, env: Mapping[str, str]) -> ContactAuthority:
    blob = " ".join([dsn, env.get("PGHOST", ""), env.get("PGUSER", ""), env.get("PGOPTIONS", "")]).lower()
    for ref, why in RETIRED_REFS.items():
        if ref in blob:
            raise ContactAuthorityError(
                "LEGACY_CONTACT_DSN_REFUSED",
                f"{source} points at {ref} ({why}). The contact graph lives in "
                f"{CANONICAL_REF}. Set {ENV_VAR} to the {CANONICAL_REF} DSN; there is no override.")
    host, user = _host_user(dsn, env)
    ref = supabase_ref(host, user)
    if ref == CANONICAL_REF:
        return ContactAuthority(dsn, source, CANONICAL, ref, host)
    if ref is not None or "supabase." in host:
        raise ContactAuthorityError(
            "NONCANONICAL_SUPABASE_REFUSED",
            f"{source} names Supabase project {ref or '(unreadable ref)'}; DeedSonar's contact "
            f"graph has one production database, {CANONICAL_REF}.")
    if host in _LOOPBACK or host == "" or host.startswith("/"):
        return ContactAuthority(dsn, source, NON_PRODUCTION, None, host)
    if (env.get(ALLOW_NONCANONICAL_VAR) or "").strip() == "1":
        return ContactAuthority(dsn, source, NONCANONICAL_ALLOWED, None, host)
    raise ContactAuthorityError(
        "UNPROVEN_CONTACT_HOST_REFUSED",
        f"{source} host {host!r} is neither {CANONICAL_REF} nor a local database, so the "
        f"DSN cannot prove which project it reaches. Use the Supabase host/pooler DSN, or set "
        f"{ALLOW_NONCANONICAL_VAR}=1 for a deliberate non-production replica.")


def resolve_contact_dsn(explicit: Optional[str] = None, *, required: bool = True,
                        env: Optional[Mapping[str, str]] = None) -> Optional[ContactAuthority]:
    """Resolve and check the contact DSN. `explicit=""` means "no database" (offline).

    Returns None only when required=False and no DSN is configured anywhere.
    Raises ContactAuthorityError for a missing (required) or refused DSN.
    """
    if env is None:
        _load_repo_dotenv()
        env = os.environ
    if explicit is not None:
        if explicit == "":
            if required:
                raise ContactAuthorityError("CONTACT_DSN_MISSING", "--database-url is empty.")
            return None
        return classify(explicit, "--database-url", env)
    for var in (ENV_VAR, FALLBACK_ENV_VAR):
        val = (env.get(var) or "").strip()
        if val and not val.startswith("replace") and "[YOUR-PASSWORD]" not in val:
            return classify(val, var, env)
    if not required:
        return None
    raise ContactAuthorityError(
        "CONTACT_DSN_MISSING",
        f"set {ENV_VAR} (the {CANONICAL_REF} DSN) in the environment or the engine .env.")


def contact_dsn_or_exit(explicit: Optional[str] = None, *, required: bool = True) -> Optional[str]:
    """CLI helper: resolve, print the authority line to stderr, or exit 2 with the refusal."""
    import sys
    try:
        auth = resolve_contact_dsn(explicit, required=required)
    except ContactAuthorityError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    if auth is None:
        return None
    print(auth.describe(), file=sys.stderr)
    return auth.dsn
