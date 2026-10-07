"""Spend authority: who may say a paid call can happen, and how a recorded yes is re-checked.

The engine never authorizes its own spend. In the organism the spend authority is
Governance: `/v2/authorize` classifies action_class "spend" as R4 and any
spend_usd > 0 as R4, which is founder-interactive (a FounderGrant can never cover
R4/R5). This module:

  * asks Governance with gov.action.facts.v1 facts that bind the job, the provider,
    the budget and a nonce (the same shape neuruh-workers git_custodian sends);
  * accepts only an ALLOW that is provably about those facts and carries a decision
    receipt (the verify_governance_allow discipline);
  * at every paid call, re-confirms the recorded authorization against Governance's
    own hash-chained receipts log, so a SPEND_AUTHORIZED line appended to the
    free-first ledger by anyone else is not authority.

The default authority is FailClosed: with no authority configured, no paid call
can ever clear. The authority is resolved from process configuration
(LANDOS_FREE_FIRST_AUTHORITY), never passed in by the code that wants to spend.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from .provenance import sha256_hex, utc_now_iso

AUTHORIZATION_VERSION = "landos.free_first.spend_authorization.v1"
FACTS_SCHEMA = "gov.action.facts.v1"
GOVERNANCE_AUTHORIZATION_SCHEMA = "gov.authorization.v1"
GOVERNANCE_NOT_CLAIMED = "NOT_CLAIMED_BY_GOVERNANCE"
GOVERNANCE_AUTHORIZE_URL = "http://127.0.0.1:8848/v2/authorize"
SPEND_ACTION_CLASS = "spend"
COMPONENT = "landos-engine.free_first"
RISK_TIERS = ("R0", "R1", "R2", "R3", "R4", "R5")


class AuthorityRefused(RuntimeError):
    def __init__(self, reason: str, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(f"{reason}: {dict(detail or {})}")
        self.reason = reason
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class SpendRequest:
    job_id: str
    fact: str
    target_key: str
    county_fips: str
    providers: Sequence[str]
    max_total_usd: float
    max_per_call_usd: Optional[float]
    waives: Mapping[str, str]
    requested_by: str
    exhaustion_verdict: str
    ledger_tip: str
    reason: str = ""


@dataclass(frozen=True)
class SpendAuthorization:
    authorization_id: str
    job_id: str
    fact: str
    providers: List[str]
    max_total_usd: float
    max_per_call_usd: Optional[float]
    waives: Dict[str, str]
    authority: str
    authority_ref: Dict[str, Any]
    issued_at: str
    expires_at: str
    requested_by: str
    facts_sha256: str
    version: str = AUTHORIZATION_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SpendAuthority(Protocol):
    name: str

    def authorize(self, request: SpendRequest) -> SpendAuthorization: ...

    def confirm(self, authorization: Mapping[str, Any], *, now: datetime) -> None: ...


class FailClosedAuthority:
    """No spend authority is configured: nothing is ever authorized or confirmed."""
    name = "FAIL_CLOSED"

    def authorize(self, request: SpendRequest) -> SpendAuthorization:
        raise AuthorityRefused("NO_SPEND_AUTHORITY_CONFIGURED",
                               {"job_id": request.job_id, "hint": "LANDOS_FREE_FIRST_AUTHORITY=governance"})

    def confirm(self, authorization: Mapping[str, Any], *, now: datetime) -> None:
        raise AuthorityRefused("NO_SPEND_AUTHORITY_CONFIGURED",
                               {"authorization_id": authorization.get("authorization_id")})


def spend_facts(request: SpendRequest, *, nonce: str, issued_at: str, source_sha: str) -> Dict[str, Any]:
    """The exact gov.action.facts.v1 for THIS spend. Governance classifies it (R4)."""
    return {
        "schema_version": FACTS_SCHEMA,
        "action_id": "spend-" + sha256_hex({"job": request.job_id, "providers": sorted(request.providers),
                                            "max": request.max_total_usd, "nonce": nonce})[:24],
        "lane": "free-first",
        "component": COMPONENT,
        "source_sha": source_sha,
        "action_class": SPEND_ACTION_CLASS,
        "summary": (f"paid enrichment {','.join(sorted(request.providers))} for {request.fact} on "
                    f"{request.target_key} (county {request.county_fips}), up to ${request.max_total_usd:.2f}; "
                    f"free path {request.exhaustion_verdict}"),
        "target": {"type": "enrichment_job", "id": request.job_id},
        "externality": "vendor",
        "reversibility": "irreversible",
        "rollback_plan": "none: a vendor charge cannot be reversed; the budget cap bounds it",
        "blast_radius": "bounded",
        "production_effect": "none",
        "spend_usd": float(request.max_total_usd),
        "precondition_hash": sha256_hex({"ledger_tip": request.ledger_tip, "waives": dict(request.waives)}),
        "nonce": nonce,
        "issued_at": issued_at,
    }


def verify_governance_allow(envelope: Any, facts: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise AuthorityRefused("GOVERNANCE_INVALID", {"why": "not an authorization envelope"})
    if envelope.get("schema_version") != GOVERNANCE_AUTHORIZATION_SCHEMA:
        raise AuthorityRefused("GOVERNANCE_INVALID", {"schema_version": envelope.get("schema_version")})
    if envelope.get("execution") != GOVERNANCE_NOT_CLAIMED:
        raise AuthorityRefused("GOVERNANCE_CLAIMS_EXECUTION")
    if envelope.get("facts_nonce") != facts["nonce"]:
        raise AuthorityRefused("GOVERNANCE_ENVELOPE_UNBOUND", {"field": "facts_nonce"})
    decision = envelope.get("decision")
    if decision == "founder_required":
        raise AuthorityRefused("GOVERNANCE_FOUNDER_REQUIRED",
                               {"risk_tier": envelope.get("risk_tier"),
                                "authorization_id": envelope.get("authorization_id")})
    if decision == "deny":
        raise AuthorityRefused("GOVERNANCE_DENY", {"reason_code": envelope.get("reason_code")})
    if decision != "allow":
        raise AuthorityRefused("GOVERNANCE_INVALID", {"decision": decision})
    if envelope.get("action_class") != facts["action_class"]:
        raise AuthorityRefused("GOVERNANCE_ENVELOPE_UNBOUND", {"field": "action_class"})
    if dict(envelope.get("target") or {}) != dict(facts["target"]):
        raise AuthorityRefused("GOVERNANCE_ENVELOPE_UNBOUND", {"field": "target"})
    if envelope.get("risk_tier") not in RISK_TIERS:
        raise AuthorityRefused("GOVERNANCE_INVALID", {"why": "no risk tier"})
    if not envelope.get("decision_receipt_hash") or not envelope.get("authorization_id"):
        raise AuthorityRefused("GOVERNANCE_INVALID", {"why": "an unreceipted allow is not authority"})
    return dict(envelope)


def read_governance_receipts(path: str | os.PathLike) -> List[Dict[str, Any]]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise AuthorityRefused("GOVERNANCE_RECEIPTS_UNREADABLE", {"path": str(p)})
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


@dataclass
class GovernanceSpendAuthority:
    """Governance is asked; its receipts log is the proof a recorded yes is real."""
    authorize_port: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    receipts_reader: Callable[[], List[Mapping[str, Any]]]
    source_sha: str
    clock: Callable[[], str] = utc_now_iso
    name: str = field(default="GOVERNANCE", init=False)

    def authorize(self, request: SpendRequest) -> SpendAuthorization:
        issued_at = self.clock()
        nonce = "ffn_" + sha256_hex({"job": request.job_id, "t": issued_at, "tip": request.ledger_tip})[:28]
        facts = spend_facts(request, nonce=nonce, issued_at=issued_at, source_sha=self.source_sha)
        envelope = verify_governance_allow(self.authorize_port(facts), facts)
        return SpendAuthorization(
            authorization_id=str(envelope["authorization_id"]), job_id=request.job_id, fact=request.fact,
            providers=sorted(request.providers), max_total_usd=float(request.max_total_usd),
            max_per_call_usd=request.max_per_call_usd, waives=dict(request.waives), authority=self.name,
            authority_ref={k: envelope.get(k) for k in ("authorization_id", "decision_receipt_hash", "receipt_seq",
                                                        "facts_nonce", "risk_tier", "decision_hash")},
            issued_at=issued_at, expires_at=str(envelope.get("expires_at") or issued_at),
            requested_by=request.requested_by, facts_sha256=sha256_hex(facts))

    def confirm(self, authorization: Mapping[str, Any], *, now: datetime) -> None:
        ref = authorization.get("authority_ref") or {}
        want = ref.get("decision_receipt_hash")
        if authorization.get("authority") != self.name or not want:
            raise AuthorityRefused("AUTHORITY_MISMATCH", {"authority": authorization.get("authority")})
        entry = next((e for e in self.receipts_reader() if e.get("entry_hash") == want), None)
        if entry is None:
            raise AuthorityRefused("GOVERNANCE_RECEIPT_NOT_FOUND", {"decision_receipt_hash": want})
        checks = {
            "kind": entry.get("kind") == "governance_authorization",
            "decision": entry.get("decision") == "allow",
            "action_class": entry.get("action_class") == SPEND_ACTION_CLASS,
            "facts_nonce": entry.get("facts_nonce") == ref.get("facts_nonce"),
            "authorization_id": entry.get("authorization_id") == authorization.get("authorization_id"),
            "target": dict(entry.get("target") or {}) == {"type": "enrichment_job", "id": authorization.get("job_id")},
        }
        failed = sorted(k for k, ok in checks.items() if not ok)
        if failed:
            raise AuthorityRefused("GOVERNANCE_RECEIPT_MISMATCH", {"fields": failed})
        expires = entry.get("expires_at")
        if expires and datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= now:
            raise AuthorityRefused("GOVERNANCE_DECISION_EXPIRED", {"expires_at": expires})


def loopback_authorize_port(url: str = GOVERNANCE_AUTHORIZE_URL, *, timeout_s: float = 5.0):
    """POST loopback /v2/authorize. Submits; never signs; never caches an allow."""
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        raise AuthorityRefused("GOVERNANCE_NOT_LOOPBACK", {"url": url})
    import urllib.request

    def _port(facts: Mapping[str, Any]) -> Mapping[str, Any]:
        req = urllib.request.Request(url, data=json.dumps({"facts": dict(facts)}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 -- any transport failure is a refusal
            raise AuthorityRefused("GOVERNANCE_UNREACHABLE", {"error": type(exc).__name__}) from exc
    return _port


_OVERRIDE: Optional[SpendAuthority] = None


def configured_authority() -> SpendAuthority:
    """The process's spend authority. FailClosed unless configured for Governance."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    if (os.environ.get("LANDOS_FREE_FIRST_AUTHORITY") or "").strip().lower() != "governance":
        return FailClosedAuthority()
    receipts = os.environ.get("GOVERNANCE_AUTHORIZE_RECEIPTS_LOG") or ""
    return GovernanceSpendAuthority(
        authorize_port=loopback_authorize_port(os.environ.get("LANDOS_GOVERNANCE_AUTHORIZE_URL", GOVERNANCE_AUTHORIZE_URL)),
        receipts_reader=lambda: read_governance_receipts(receipts),
        source_sha=os.environ.get("LANDOS_ENGINE_SOURCE_SHA", "unattested"))


def install_authority_for_tests(authority: Optional[SpendAuthority]) -> None:
    """Test seam only (courts install a deterministic authority, then remove it)."""
    global _OVERRIDE
    _OVERRIDE = authority
