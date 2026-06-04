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
