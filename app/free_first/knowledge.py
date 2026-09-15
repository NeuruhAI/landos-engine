"""FREE-FIRST knowledge: which free source can answer which enrichment fact for
which jurisdiction, and in what order -- read from the engine's own registers,
never restated.

Sources of truth (unchanged; this module only reads them):

  * app/providers/config.py              per-county free parcel tiers (county REST,
                                         FetchGIS, Colligo) and the chain order
  * app/free_routes/registry.py          the $0 contact / heir routes T1..T10, I1..I5
                                         (free_routes_register.v1): mode, halt, human step
  * app/resolution/source_procedure.py   county procedures (NC eCourts smart search)
  * app/normalizers/reportall_owner.py   the counties ReportAll (paid) is configured for

A ladder is computed HERE from those registers. A caller may add free sources it
knows about (a national registry row, a new county adapter): additions only make
the ladder longer. They never remove a canonical step and never sort ahead of
jurisdiction-specific knowledge for the same fact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from app.fips import state_county_fips
from app.free_routes.registry import FREE_ROUTES_REGISTER_VERSION, ROUTES, Route
from app.providers.config import (COLLIGO_ENDPOINTS, COUNTY_REST_ENDPOINTS, FETCHGIS_ENDPOINTS,
                                  PARCEL_PROVIDER_CHAIN)
from app.resolution.source_procedure import PROCEDURES
from app.resolution.sources import NC_COUNTIES

KNOWLEDGE_VERSION = "landos.free_first.knowledge.v1"

AUTOMATED, MANUAL = "AUTOMATED", "MANUAL"
#: Scope order is the jurisdiction-knowledge rule: what we already hold, then the
#: county's own procedure, then the state's, then anything national / generic.
SCOPES = ("INTERNAL", "COUNTY", "STATE", "NATIONAL")

#: The enrichment facts the ladder can be asked for.
FACTS = {
    "parcel.owner_identity": "who holds title to the parcel (owner names, owner kind)",
    "contact.mailing_address": "the owner's current mailing address",
    "contact.phone": "a phone number for the owner / party",
    "contact.email": "an email address for the owner / party",
    "contact.heirs": "the heirs / devisees of a deceased owner",
    "contact.legal_authority": "the personal representative / executor / administrator",
    "contact.entity_principal": "registered agent / officer of an owning entity",
    "contact.dnc_status": "Do-Not-Call registry status of a known phone",
    "contact.phone_line_type": "line type / validity of a known phone",
}

#: What each free route can establish (from each Route.yields in free_routes_register.v1).
#: T9 (USPS) standardises an address it is given and I4 / I5 order and re-fire other
#: routes; none of them discovers a fact, so none of them is a ladder step.
ROUTE_FACTS: Mapping[str, Tuple[str, ...]] = {
    "T1": ("contact.legal_authority", "contact.heirs", "contact.mailing_address"),
    "T2": ("contact.legal_authority", "contact.mailing_address"),
    "T3": ("contact.heirs",),
    "T4": ("contact.heirs",),
    "T5": ("contact.entity_principal", "contact.mailing_address"),
    "T6": ("contact.phone", "contact.mailing_address"),
    "T7": ("parcel.owner_identity", "contact.mailing_address"),
    "T8": ("contact.mailing_address",),
    "T10": ("contact.phone",),
    "I2": ("contact.phone",),
    "I3": ("contact.phone",),
}

#: Where each route's SOURCE applies -- not where a lane has already run. The registry
#: is NC-built: eCourts / ncnotices / SOS are statewide; NCSBE publishes the voter file
#: for all 100 NC counties, so T6 (and the identity graph the NC lanes feed, I2 / I3)
#: applies in every NC county, and whether the lane has covered a parcel is the
#: executor's answer (UNWIRED until it has: the free path stays open, never vacuously
#: exhausted). T7 / T8 name their counties; people-search and obituaries are national.
_NC_ALL = state_county_fips("NC")
ROUTE_COVERAGE: Mapping[str, Tuple[str, frozenset]] = {
    "T1": ("STATE", frozenset({"NC"})),
    "T2": ("STATE", frozenset({"NC"})),
    "T3": ("STATE", frozenset({"NC"})),
    "T4": ("NATIONAL", frozenset()),
    "T5": ("STATE", frozenset({"NC"})),
    "T6": ("COUNTY", _NC_ALL),
    "T7": ("COUNTY", frozenset({NC_COUNTIES["Wake"], NC_COUNTIES["Mecklenburg"], NC_COUNTIES["Durham"]})),
    "T8": ("COUNTY", frozenset({NC_COUNTIES["Wake"], NC_COUNTIES["Mecklenburg"]})),
    "T10": ("NATIONAL", frozenset()),
    "I2": ("INTERNAL", _NC_ALL),
    "I3": ("INTERNAL", _NC_ALL),
}

#: The source family whose learned procedure teaches a route (jurisdiction knowledge
#: attached to the step), keyed as app.resolution.source_procedure.PROCEDURES is.
ROUTE_PROCEDURE_FAMILY = {"T1": "PROBATE_INDEX"}

_PARCEL_FACTS = ("parcel.owner_identity", "contact.mailing_address")
_FREE_TIERS = {"county_rest": COUNTY_REST_ENDPOINTS, "fetchgis": FETCHGIS_ENDPOINTS,
               "colligo": COLLIGO_ENDPOINTS}

#: How long a terminal attempt keeps a source exhausted before it must be re-tried.
#: A live query ages fast; a published list is as fresh as its publication.
DEFAULT_TTL_DAYS = {"PARCEL_TIER": 30, "FREE_ROUTE": 30, "EXTRA": 30}


@dataclass(frozen=True)
class PaidProvider:
    provider: str
    facts: Tuple[str, ...]
    hosts: Tuple[str, ...]
    unit_cost_usd: Optional[float]      # None = UNPRICED, which is never free
    cost_basis: str
    call_sites: Tuple[str, ...]          # module:function that performs the paid HTTP


PAID_PROVIDERS: Mapping[str, PaidProvider] = {p.provider: p for p in (
    PaidProvider("reportall", _PARCEL_FACTS, ("reportallusa.com",), None,
                 "metered vendor API; alltime quota per key (UNPRICED)",
                 ("app/normalizers/reportall_owner.py:ReportAllOwnerNormalizer._search",)),
    PaidProvider("regrid", _PARCEL_FACTS, ("regrid.com",), None,
                 "paid national parcel API; not implemented, key-gated (UNPRICED)",
                 ("app/providers/regrid_provider.py:RegridProvider._gate",)),
    PaidProvider("tracerfy", ("contact.phone", "contact.email"), ("tracerfy.com",), 0.02,
                 "tracerfy.com/pricing: 1 credit = $0.02 per record",
                 ("scripts/skip_trace_tracerfy.py:submit_trace",
                  "scripts/export_outreach.py:tracerfy_batch_trace")),
    PaidProvider("tracerfy_dnc", ("contact.dnc_status",), ("tracerfy.com",), 0.02,
                 "separate ~$0.02 per phone DNC scrub",
                 ("scripts/skip_trace_tracerfy.py:submit_dnc",)),
    PaidProvider("batchdata", ("contact.phone", "contact.email"), ("batchdata.com",), 0.07,
                 "trial export pricing ballpark, per lookup",
                 ("scripts/skip_trace_batchdata.py:_api_call",)),
    PaidProvider("twilio_lookup", ("contact.phone_line_type",), ("lookups.twilio.com",), None,
                 "Twilio Lookup v2 line-type intelligence (UNPRICED here)",
                 ("scripts/contact_rank.py:twilio_lookup",)),
)}

PAID_HOSTS: Tuple[str, ...] = tuple(sorted({h for p in PAID_PROVIDERS.values() for h in p.hosts}))


@dataclass(frozen=True)
class LadderStep:
    step_id: str              # stable id of the step on the ladder
    source_id: str            # the registry id it reads: tier name, route id, caller source id
    source_kind: str          # PARCEL_TIER | FREE_ROUTE | EXTRA
    mode: str                 # AUTOMATED | MANUAL
    scope: str                # INTERNAL | COUNTY | STATE | NATIONAL
    order: int                # yield order inside its scope
    halt: str = ""            # H-12 when a human step is required before the source can answer
    human_step: str = ""
    procedure: str = ""       # the county procedure that teaches this step, when one exists
    provenance_url: str = ""
    license: str = ""
    ttl_days: int = 30
    why: str = ""

    def sort_key(self) -> tuple:
        return (SCOPES.index(self.scope), 0 if self.mode == AUTOMATED else 1, self.order, self.step_id)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Ladder:
    fact: str
    state: str
    county_fips: str
    steps: Tuple[LadderStep, ...]
    knowledge_version: str = KNOWLEDGE_VERSION
    routes_version: str = FREE_ROUTES_REGISTER_VERSION
    digest: str = field(default="")

    def step(self, step_id: str) -> LadderStep:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        raise KeyError(step_id)

    def to_dict(self) -> dict:
        return {"fact": self.fact, "state": self.state, "county_fips": self.county_fips,
                "steps": [s.to_dict() for s in self.steps], "knowledge_version": self.knowledge_version,
                "routes_version": self.routes_version, "digest": self.digest}


def _digest(steps: Sequence[LadderStep], fact: str, state: str, county_fips: str) -> str:
    body = json.dumps({"fact": fact, "state": state, "county_fips": county_fips,
                       "steps": [s.to_dict() for s in steps], "v": KNOWLEDGE_VERSION},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _route_step(route: Route, scope: str, fact: str) -> LadderStep:
    procedure = ""
    family = ROUTE_PROCEDURE_FAMILY.get(route.route_id)
    if family:
        learned = PROCEDURES.get(family)
        if learned is None:
            raise RuntimeError(f"route {route.route_id} names unknown procedure family {family}")
        procedure = f"{learned.name}@{learned.version}"
    return LadderStep(
        step_id=f"route.{route.route_id}", source_id=route.route_id, source_kind="FREE_ROUTE",
        mode=AUTOMATED if route.mode == "AUTOMATED" else MANUAL, scope=scope, order=route.yield_order,
        halt=route.halt, human_step=route.human_step, procedure=procedure,
        provenance_url=route.provenance_url, license=route.license, ttl_days=DEFAULT_TTL_DAYS["FREE_ROUTE"],
        why=f"{route.name} yields {route.yields!r} ({fact})")


def free_route_steps(fact: str, *, state: str, county_fips: str) -> list:
    out = []
    for route in ROUTES:
        if fact not in ROUTE_FACTS.get(route.route_id, ()):
            continue
        if route.mode == "BLOCKED":
            continue                                   # not a free path (H-11)
        scope, members = ROUTE_COVERAGE[route.route_id]
        if scope == "STATE" and state not in members:
            continue
        if scope in ("COUNTY", "INTERNAL") and county_fips not in members:
            continue
        out.append(_route_step(route, scope, fact))
    return out


def parcel_tier_steps(fact: str, *, county_fips: str) -> list:
    """The county's free parcel tiers in the chain's own order; paid tiers are not steps."""
    if fact not in _PARCEL_FACTS:
        return []
    order = PARCEL_PROVIDER_CHAIN.get(county_fips, PARCEL_PROVIDER_CHAIN["default"])
    out = []
    for i, tier in enumerate(order):
        if tier == "local_cache":
            out.append(LadderStep(
                step_id="parcel.local_cache", source_id="local_cache", source_kind="PARCEL_TIER",
                mode=AUTOMATED, scope="INTERNAL", order=i, ttl_days=1,
                why="the engine's own owner_leads cache answers first (already paid for or already free)"))
            continue
        endpoints = _FREE_TIERS.get(tier)
        if endpoints is None:
            continue                                    # reportall / regrid: paid tiers
        cfg = endpoints.get(county_fips)
        if not cfg or not cfg.get("verified", True):
            continue                                    # no verified free endpoint for this county
        out.append(LadderStep(
            step_id=f"parcel.{tier}.{county_fips}", source_id=tier, source_kind="PARCEL_TIER",
            mode=AUTOMATED, scope="COUNTY", order=i,
            provenance_url=cfg.get("url") or cfg.get("base_url") or cfg.get("search_url") or "",
            license="county-published public GIS", ttl_days=DEFAULT_TTL_DAYS["PARCEL_TIER"],
            why=f"verified {tier} endpoint for county {county_fips} (app/providers/config.py)"))
    return out


def _extra_step(src: Mapping, fact: str) -> LadderStep:
    scope = str(src.get("scope") or "NATIONAL").upper()
    if scope not in SCOPES or scope == "INTERNAL":
        scope = "NATIONAL"
    mode = MANUAL if str(src.get("mode") or "").upper() == MANUAL else AUTOMATED
    sid = str(src["source_id"])
    return LadderStep(
        step_id=f"extra.{sid}", source_id=sid, source_kind="EXTRA", mode=mode, scope=scope,
        order=int(src.get("order") or 100), halt="H-12" if mode == MANUAL else "",
        human_step=str(src.get("human_step") or ""), provenance_url=str(src.get("source_url") or ""),
        license=str(src.get("license") or ""), ttl_days=int(src.get("ttl_days") or DEFAULT_TTL_DAYS["EXTRA"]),
        why=str(src.get("why") or f"caller-declared free source for {fact}"))


def plan_ladder(fact: str, *, state: str, county_fips: str,
                extra_free_sources: Iterable[Mapping] = ()) -> Ladder:
    """The ordered free ladder for one fact in one jurisdiction.

    ``extra_free_sources`` rows ({source_id, scope, mode, ...}) are appended; a caller
    can never drop or re-rank a canonical step, and an extra claiming COUNTY scope still
    sorts after the canonical COUNTY steps of the same mode (its default order is 100)."""
    if fact not in FACTS:
        raise ValueError(f"unknown enrichment fact {fact!r}; known: {sorted(FACTS)}")
    state = (state or "").strip().upper()
    county_fips = str(county_fips or "").strip().zfill(5)
    steps = parcel_tier_steps(fact, county_fips=county_fips) + free_route_steps(
        fact, state=state, county_fips=county_fips)
    canonical_ids = {s.step_id for s in steps}
    for src in extra_free_sources:
        if fact not in (src.get("facts") or ()):
            continue
        step = _extra_step(src, fact)
        if step.step_id not in canonical_ids:
            steps.append(step)
            canonical_ids.add(step.step_id)
    steps.sort(key=LadderStep.sort_key)
    frozen = tuple(steps)
    return Ladder(fact, state, county_fips, frozen, digest=_digest(frozen, fact, state, county_fips))


def paid_providers_for(fact: str) -> list:
    return [p for p in PAID_PROVIDERS.values() if fact in p.facts]


def knowledge_snapshot() -> dict:
    """What the engine knows, for receipts and for the national registry read model."""
    return {
        "knowledge_version": KNOWLEDGE_VERSION, "routes_version": FREE_ROUTES_REGISTER_VERSION,
        "facts": dict(FACTS), "route_facts": {k: list(v) for k, v in ROUTE_FACTS.items()},
        "route_coverage": {k: {"scope": s, "members": sorted(m)} for k, (s, m) in ROUTE_COVERAGE.items()},
        "free_parcel_tier_counties": {t: sorted(e) for t, e in _FREE_TIERS.items()},
        "paid_providers": {k: asdict(p) for k, p in PAID_PROVIDERS.items()},
    }
