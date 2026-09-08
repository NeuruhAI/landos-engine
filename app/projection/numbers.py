"""Shared numeric / APN helpers for derive-and-project unlocks."""
from __future__ import annotations

from typing import Any, Iterable, Optional


def as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return float(value)
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL", "UNKNOWN"}:
        return None
    cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def first_present(row: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def first_number(row: dict, keys: Iterable[str]) -> Optional[float]:
    raw = first_present(row, keys)
    return as_number(raw)


def first_text(row: dict, keys: Iterable[str]) -> Optional[str]:
    raw = first_present(row, keys)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def apn_keys(apn: Any) -> list[str]:
    """Match keys for an APN: raw, upper, digits, first-10 digits, alnum."""
    raw = str(apn or "").strip()
    if not raw:
        return []
    keys = {raw, raw.upper()}
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        keys.add(digits)
        if len(digits) >= 10:
            keys.add(digits[:10])
    alnum = "".join(ch for ch in raw.upper() if ch.isalnum())
    if alnum:
        keys.add(alnum)
    return list(keys)


def row_apn(row: dict) -> str:
    return str(
        first_present(row, ("apn", "parcel_apn", "parcel_id", "pin")) or ""
    ).strip()
