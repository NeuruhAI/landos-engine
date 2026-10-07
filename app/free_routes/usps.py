"""T9: USPS Addresses API v3 — standardize and validate every mailing address,
flag undeliverable. Free with the default product; needs a USPS Business Account
+ App + OAuth client credentials (human step, H-12) held in the engine env as
USPS_CONSUMER_KEY / USPS_CONSUMER_SECRET. NCOA is paid and never called.

Each validated address is stamped source=USPS_ADDRESS_V3, source_as_of = the
validation date (the day USPS said it), observed_at = now, confidence from DPV."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional

SOURCE = "USPS_ADDRESS_V3"
TOKEN_URL = "https://apis.usps.com/oauth2/v3/token"
ADDRESS_URL = "https://apis.usps.com/addresses/v3/address"
PROVENANCE_URL = "https://developers.usps.com/addressesv3"
HUMAN_STEP = ("Register a USPS Business Account at https://cop.usps.com, create an App on the Developer Portal, "
              "copy its Consumer Key and Consumer Secret into the engine env as USPS_CONSUMER_KEY / "
              "USPS_CONSUMER_SECRET; the adapter then runs unattended (default product, quota increase by request).")


@dataclass
class ValidatedAddress:
    input_key: str
    standardized: Optional[str]
    city: Optional[str]
    state: Optional[str]
    zip5: Optional[str]
    zip4: Optional[str]
    dpv_confirmation: Optional[str]     # Y / D / S / N per USPS DPV
    deliverable: Optional[bool]
    vacant: Optional[bool]
    source: str
    source_as_of: str
    observed_at: str
    confidence: Optional[str]
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def credentials_present(env: Dict[str, str] = None) -> bool:
    env = env if env is not None else os.environ
    return bool((env.get("USPS_CONSUMER_KEY") or "").strip() and (env.get("USPS_CONSUMER_SECRET") or "").strip())


def _post_form(url: str, data: Dict[str, str], timeout: int = 30) -> Dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url: str, token: str, timeout: int = 30) -> Dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_token(env: Dict[str, str] = None, post: Callable[[str, Dict[str, str]], Dict] = _post_form) -> str:
    env = env if env is not None else os.environ
    data = {"grant_type": "client_credentials", "client_id": env["USPS_CONSUMER_KEY"],
            "client_secret": env["USPS_CONSUMER_SECRET"], "scope": "addresses"}
    return post(TOKEN_URL, data)["access_token"]


def dpv_to_confidence(dpv: Optional[str]) -> Optional[str]:
    return {"Y": "HIGH", "D": "MEDIUM", "S": "MEDIUM", "N": "LOW"}.get((dpv or "").upper()) if dpv else None


def validate_one(input_key: str, street: str, city: str = "", state: str = "", zip5: str = "", *, token: str,
                 today: str, now_iso: str, get: Callable[[str, str], Dict] = _get_json) -> ValidatedAddress:
    q = urllib.parse.urlencode({k: v for k, v in {"streetAddress": street, "city": city, "state": state,
                                                  "ZIPCode": zip5}.items() if v})
    try:
        data = get(f"{ADDRESS_URL}?{q}", token)
    except Exception as e:  # noqa: BLE001 — the receipt names the failure, never a fabricated address
        return ValidatedAddress(input_key, None, None, None, None, None, None, None, None, SOURCE, today, now_iso, None,
                                error=str(e)[:200])
    addr = data.get("address", {}) or {}
    info = data.get("additionalInfo", {}) or {}
    dpv = info.get("DPVConfirmation")
    std = " ".join(x for x in [addr.get("streetAddress"), addr.get("secondaryAddress")] if x)
    return ValidatedAddress(input_key, std or None, addr.get("city"), addr.get("state"), addr.get("ZIPCode"),
                            addr.get("ZIPPlus4"), dpv, (dpv in ("Y", "D", "S")) if dpv else None,
                            (info.get("vacant") == "Y") if info.get("vacant") is not None else None,
                            SOURCE, today, now_iso, dpv_to_confidence(dpv))


def validate_many(rows: List[Dict[str, str]], *, env: Dict[str, str] = None, today: str, now_iso: str,
                  post=_post_form, get=_get_json, delay_s: float = 0.2) -> List[ValidatedAddress]:
    """rows: [{key, street, city, state, zip5}] → validated list. Raises if credentials are absent
    (callers turn that into an H-12 receipt with HUMAN_STEP)."""
    env = env if env is not None else os.environ
    if not credentials_present(env):
        raise PermissionError("USPS credentials absent: " + HUMAN_STEP)
    token = get_token(env, post)
    out = []
    for r in rows:
        out.append(validate_one(r["key"], r.get("street", ""), r.get("city", ""), r.get("state", ""), r.get("zip5", ""),
                                token=token, today=today, now_iso=now_iso, get=get))
        if delay_s:
            time.sleep(delay_s)
    return out
