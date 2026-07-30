"""LandOS/BLACK-compatible canonical JSON and SHA-256 seals."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def canonical_json_bytes(value: Any) -> bytes:
    """Stable UTF-8 JSON bytes; rejects non-finite JSON numbers."""

    return json.dumps(
        value,
        allow_nan=False,
        # Match app.receipt/BLACK court canonicalization exactly so artifact
        # seals compose into the same ordered proof chain.
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_sha256(value: Mapping[str, Any]) -> str:
    """Hash a contract artifact after excluding its self-referential seal."""

    payload = dict(value)
    payload.pop("content_sha256", None)
    return sha256_hex(payload)


def verify_content_hash(value: Mapping[str, Any]) -> bool:
    claimed = value.get("content_sha256")
    return isinstance(claimed, str) and claimed == content_sha256(value)
