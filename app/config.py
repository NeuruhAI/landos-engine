"""
config.py — loads environment variables from .env and validates them.

Why this file exists: every secret the service needs (the database
connection string, the bridge secret) is loaded here in ONE place. If
something is missing or you forgot to fill it in, the service refuses to
start and tells you exactly which variable is wrong — instead of crashing
later with a confusing error.
"""
import os
from dotenv import load_dotenv

# Reads the .env file sitting next to this project and loads each line
# (KEY=value) into the environment so os.environ can see them.
load_dotenv()


def _require(name: str) -> str:
    """Return the env var, or raise a clear error if it's missing/placeholder."""
    v = os.environ.get(name)
    if not v or v.startswith("replace") or "[YOUR-PASSWORD]" in v:
        raise RuntimeError(
            f"Environment variable {name} is missing or still set to the "
            f"placeholder. Open the .env file in this folder and set a real value."
        )
    return v


# The full Postgres connection string for your Supabase database.
DATABASE_URL = _require("DATABASE_URL")

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
