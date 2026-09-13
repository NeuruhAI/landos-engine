"""
config.py — loads environment variables from .env and validates them.

Why this file exists: every secret the service needs (the database
connection string, the bridge secret) is loaded here in ONE place. If
something is missing or you forgot to fill it in, the service refuses to
start and tells you exactly which variable is wrong — instead of crashing
later with a confusing error.
"""
import os
from typing import Mapping, Optional

from dotenv import load_dotenv

from .contact_authority import CANONICAL_REF, RETIRED_REFS

# Reads the .env file sitting next to this project and loads each line
# (KEY=value) into the environment so os.environ can see them.
load_dotenv()

# A deliberate forensic READ of the retired spine (parity checks) sets this to 1.
# Nothing in production sets it; without it the legacy project cannot become the
# engine's database at all.
ALLOW_LEGACY_SPINE_VAR = "LANDOS_ALLOW_LEGACY_SPINE"


def _is_set(v: Optional[str]) -> bool:
    if not v:
        return False
    return not v.startswith("replace") and "[YOUR-PASSWORD]" not in v


def _require(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Return the env var, or raise a clear error if it's missing/placeholder."""
    v = (os.environ if env is None else env).get(name)
    if v is None or not _is_set(v):
        raise RuntimeError(
            f"Environment variable {name} is missing or still set to the "
            f"placeholder. Open the .env file in this folder and set a real value."
        )
    return v


def refuse_legacy_spine(dsn: str, env: Mapping[str, str]) -> None:
    """The retired spine may not be the engine's database (T4 topology, 2026-09-13).

    Before this guard a stale DATABASE_URL was a silent path back to lvdi: the
    scorer kept writing lvdi landos.scores for four days after the cutover
    (efxo 5,230 vs lvdi 5,277). The DSN, PGHOST and PGUSER are all checked.
    """
    blob = " ".join([dsn, env.get("PGHOST", ""), env.get("PGUSER", "")]).lower()
    for ref, why in RETIRED_REFS.items():
        if ref in blob:
            if (env.get(ALLOW_LEGACY_SPINE_VAR) or "").strip() == "1":
                return
            raise RuntimeError(
                f"LEGACY_SPINE_REFUSED: the engine database resolved to {ref} ({why}). "
                f"Production is {CANONICAL_REF}: set DEEDSONAR_DATABASE_URL to that DSN. "
                f"A deliberate forensic READ of the legacy spine must set "
                f"{ALLOW_LEGACY_SPINE_VAR}=1.")


def resolve_engine_database_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the engine DB from the canonical DeedSonar DSN first.

    Since the 2026-09-10 production cutover, efxowqudmsfqjmbeidjc is the
    single production authority for both the DeedSonar CRM tables and the
    LandOS schema used by sync_to_crm. Prefer DEEDSONAR_DATABASE_URL so a
    stale legacy DATABASE_URL cannot silently keep the engine on lvdi.
    The legacy-named DATABASE_URL is a fallback only when the canonical
    variable is genuinely absent — and whichever variable wins, the retired
    lvdi project is refused (refuse_legacy_spine).
    """
    if env is None:
        env = os.environ
    canonical = env.get("DEEDSONAR_DATABASE_URL")
    dsn = canonical if canonical is not None and _is_set(canonical) else _require("DATABASE_URL", env)
    refuse_legacy_spine(dsn, env)
    return dsn


# The ONE canonical production Postgres connection string.
# DEEDSONAR_DATABASE_URL wins over the historical DATABASE_URL so the engine,
# sync_to_crm, and contact tooling converge on the same efxo production authority.
DATABASE_URL = resolve_engine_database_url()

# The shared secret APG must send in the X-LANDOS-SECRET header to call us.
LANDOS_SECRET = _require("LANDOS_SECRET")

# The local port the service listens on. Defaults to 8400.
PORT = int(os.environ.get("PORT", "8400"))

# --- DS-07 AI Insights (NEURUH IP) — model routing + the spend meter -------------------
# Local (Ollama, Contabo box) is $0 and used for bulk / low-score. The paid model (Claude)
# handles high-score / active-pipeline leads and is the ONLY path the meter gates.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")            # empty => Claude tier off
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# Rough Claude token prices ($ per 1M tokens) — override per model. Used to ESTIMATE the
# pre-call cost (meter gate) and to price the ACTUAL usage the API returns.
INSIGHT_PRICE_IN_PER_MTOK = float(os.environ.get("INSIGHT_PRICE_IN_PER_MTOK", "1.0"))
INSIGHT_PRICE_OUT_PER_MTOK = float(os.environ.get("INSIGHT_PRICE_OUT_PER_MTOK", "5.0"))
# The meter. Hard monthly cap, a per-run cap, and a kill switch. Exceeding any => honest
# offline (never a silent degrade, never a fabricated brief).
INSIGHT_MONTHLY_CAP_USD = float(os.environ.get("INSIGHT_MONTHLY_CAP_USD", "25"))
INSIGHT_PER_RUN_CAP_USD = float(os.environ.get("INSIGHT_PER_RUN_CAP_USD", "0.50"))
INSIGHT_KILL_SWITCH = os.environ.get("INSIGHT_KILL_SWITCH", "").strip().lower() in ("1", "true", "on", "yes")
# Board score at/above which a lead routes to the paid model.
INSIGHT_HIGH_SCORE = float(os.environ.get("INSIGHT_HIGH_SCORE", "70"))
# When the model is unreachable / off, serve the deterministic grounded brief (labeled
# model='deterministic') instead of a 503. Default off = strict honest-offline per spec.
INSIGHT_FALLBACK_DETERMINISTIC = os.environ.get(
    "INSIGHT_FALLBACK_DETERMINISTIC", "").strip().lower() in ("1", "true", "on", "yes")
