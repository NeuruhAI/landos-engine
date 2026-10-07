"""Outbound fetch for official $0 public-record files only.

Paid skiptrace, people-search, and arbitrary operator URLs are refused.
This is numbering-plan / public-file retrieval, not a callable claim.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALLOWED_FETCH_PREFIXES = (
    "https://s3.amazonaws.com/dl.ncsbe.gov/data/",
    "https://www.irs.gov/pub/irs-soi/",
    "https://apps.irs.gov/pub/epostcard/990/",
    "https://irs-form-990.s3.amazonaws.com/",
    # Census ZCTA gazetteer: the ZIP-locality arbiter. It is deliberately a
    # DIFFERENT authority from NCSBE, so a voter-file match can never be
    # validated by the voter file itself.
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/",
)

USER_AGENT = "neuruh-landos-free-contact/t20d"


def official_url_allowed(url: str) -> bool:
    text = (url or "").strip()
    if not text.startswith("https://"):
        return False
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    return any(text.startswith(prefix) for prefix in ALLOWED_FETCH_PREFIXES)


def fetch_official_bytes(url: str, timeout: int = 12) -> Optional[bytes]:
    if not official_url_allowed(url):
        return None
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — allowlist gated above
            final = resp.geturl()
            if final and not official_url_allowed(final):
                return None
            return resp.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cache-first, hash-pinned source custody.
#
# An input is never silently replaced. A cached file is reused as-is; a
# re-fetch that changes the digest is recorded as SOURCE_DRIFT, never
# overwritten in place.
# ---------------------------------------------------------------------------

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

CUSTODY_FILENAME = "free_source_custody.json"

FETCH_STATES = (
    "CACHED_UNCHANGED",     # local file present, digest matches the pinned receipt
    "CACHED_UNPINNED",      # local file present, no prior receipt — pin it now
    "FETCHED_NEW",          # was missing, downloaded from an allowlisted official URL
    "SOURCE_DRIFT",         # re-fetch produced a different digest than the pin
    "SOURCE_NOT_AVAILABLE", # missing locally and not retrievable at $0
    "REFUSED_NOT_OFFICIAL", # URL is not on the official allowlist
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_custody(path: Path) -> Dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    sources = data.get("sources", data)
    return sources if isinstance(sources, dict) else {}


def write_custody(path: Path, sources: Dict[str, dict], *, provider_spend: str = "$0.00") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "id": "free_source_custody.v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider_spend": provider_spend,
        "paid_provider_calls": 0,
        "sources": sources,
    }, indent=2, sort_keys=True) + "\n")
    return p


def ensure_official_source(
    local_path: Path,
    url: str,
    *,
    custody: Dict[str, dict],
    family: str,
    allow_download: bool = True,
) -> dict:
    """Resolve one official free input. Cache wins; drift is recorded, not applied."""
    path = Path(local_path)
    key = path.name
    pinned = custody.get(key, {}) or {}
    pinned_sha = str(pinned.get("sha256") or "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if path.exists():
        sha = sha256_file(path)
        if pinned_sha and sha != pinned_sha:
            state = "SOURCE_DRIFT"
        elif pinned_sha:
            state = "CACHED_UNCHANGED"
        else:
            state = "CACHED_UNPINNED"
        record = {
            "family": family,
            "path": str(path),
            "url": url,
            "sha256": sha,
            "prior_sha256": pinned_sha,
            "bytes": path.stat().st_size,
            "state": state,
            "observed_at": now,
            "downloaded": False,
        }
        custody[key] = record
        return record

    if not official_url_allowed(url):
        record = {
            "family": family, "path": str(path), "url": url, "sha256": "",
            "state": "REFUSED_NOT_OFFICIAL", "observed_at": now, "downloaded": False,
        }
        custody[key] = record
        return record

    if not allow_download:
        record = {
            "family": family, "path": str(path), "url": url, "sha256": "",
            "state": "SOURCE_NOT_AVAILABLE", "observed_at": now, "downloaded": False,
        }
        custody[key] = record
        return record

    payload = fetch_official_bytes(url)
    if payload is None:
        record = {
            "family": family, "path": str(path), "url": url, "sha256": "",
            "state": "SOURCE_NOT_AVAILABLE", "observed_at": now, "downloaded": False,
        }
        custody[key] = record
        return record

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    record = {
        "family": family,
        "path": str(path),
        "url": url,
        "sha256": sha,
        "prior_sha256": pinned_sha,
        "bytes": len(payload),
        "state": "SOURCE_DRIFT" if (pinned_sha and sha != pinned_sha) else "FETCHED_NEW",
        "observed_at": now,
        "downloaded": True,
    }
    custody[key] = record
    return record


def drifted_sources(custody: Dict[str, dict]) -> List[str]:
    return sorted(k for k, v in custody.items() if (v or {}).get("state") == "SOURCE_DRIFT")
