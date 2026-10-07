"""Estate identity hashing for free-contact delta refresh.

An identity is the candidate plus the owner/address tuple. candidate_id alone
is not enough: a revised estate may reuse an id with a different owner.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Optional


IDENTITY_FIELDS = (
    "candidate_id",
    "owner_name",
    "mailing_address",
    "mailing_city",
    "mailing_state",
    "mailing_zip",
    "property_address_raw",
    "property_address_normalized",
    "property_zip",
)


def _norm(value: Optional[str]) -> str:
    return " ".join(str(value or "").strip().upper().split())


def identity_payload(row: dict) -> str:
    return "|".join(_norm(row.get(field)) for field in IDENTITY_FIELDS)


def identity_tuple_hash(row: dict) -> str:
    return hashlib.sha256(identity_payload(row).encode("utf-8")).hexdigest()


def estate_revision(paths: Iterable[Path]) -> str:
    """Stable hash of the CURRENT master CSVs (name + content), not the match run."""
    digest = hashlib.sha256()
    for path in sorted(Path(p) for p in paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest.update(h.digest())
    return digest.hexdigest()


def master_csv_paths(estate_dir: Path) -> list[Path]:
    return sorted(estate_dir.glob("*/*/master/*_master_*.csv")) + sorted(
        estate_dir.glob("*/master/*_master_*.csv")
    )
