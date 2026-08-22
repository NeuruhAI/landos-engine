"""Deterministic opportunity intelligence between raw signals and action.

Recipe Engine sends a versioned JSON recipe; LandOS evaluates it and returns a
portable, tamper-evident result.  This module deliberately does not learn or
infer weights.  Every score point comes from one named recipe rule, every
confidence point comes from declared evidence coverage, and every missing or
conflicted material factor is returned as ``UNKNOWN`` with its possible delta.

The T50 property-case seam is consumed directly: research-attempt result states
are normalized through :mod:`app.case.research_result`, while their original
payloads and all caller-supplied lineage refs are copied byte-for-byte at the
Python data-model level into the opportunity genome.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Mapping, Sequence

from app.case.research_result import (
    RESEARCH_RESULT_CONTRACT_VERSION,
    generic_result_state,
    is_evidential,
    is_positive,
    supports_replay_suppression_generic,
)
from app.receipt import section_hash
from app.resolution.router import ROUTER_VERSION, next_best_source as resolution_next_source


RECIPE_CONTRACT_VERSION = "neuruh.opportunity-intelligence.recipe.v1"
INPUT_CONTRACT_VERSION = "neuruh.opportunity-intelligence.input.v1"
RESULT_CONTRACT_VERSION = "neuruh.opportunity-intelligence.result.v1"
GENOME_CONTRACT_VERSION = "neuruh.opportunity-intelligence.genome.v1"
RECEIPT_CONTRACT_VERSION = "neuruh.opportunity-intelligence.receipt.v1"
PROPERTY_POPULATION_CONTRACT_VERSION = (
    "neuruh.opportunity-intelligence.property-population.v1"
)
SOURCE_FACTORY_RUN_RECEIPT_VERSION = "source_factory.run_receipt.v1"
SOURCE_FACTORY_ADAPTER_CONTRACT_VERSION = (
    "neuruh.opportunity-intelligence.source-factory-adapter.v1"
)
SCORE_VERSION = "landos.opportunity-score.v1"

ASSET_CLASSES = (
    "waterfront",
    "acreage",
    "vacant_land",
    "residential",
    "commercial",
    "unknown",
)
SIGNAL_STATES = frozenset({"PRESENT", "ABSENT", "UNKNOWN", "CONFLICTED"})
CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW", "UNKNOWN"})
READINESS_FACT_STATES = frozenset({"YES", "NO", "UNKNOWN"})
PROPERTY_IDENTITY_KINDS = frozenset({"parcel", "address", "unresolved"})

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_WS = re.compile(r"\s+")
_STREET_SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR", "BOULEVARD": "BLVD",
    "PLACE": "PL", "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "TRAIL": "TRL", "WAY": "WAY",
}
_DIRECTIONAL = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}
LINEAGE_LIST_FIELDS = (
    "evidence_refs",
    "contact_evidence_refs",
    "source_families",
    "relationship_node_refs",
    "relationship_edge_refs",
    "quarantined_identity_refs",
)


class OpportunityIntelligenceError(ValueError):
    """The request cannot be evaluated without inventing a fact or policy."""


def _nonempty(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OpportunityIntelligenceError(f"{path} must be non-empty")
    return text


def _as_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise OpportunityIntelligenceError(f"{path} must be a list")
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpportunityIntelligenceError(f"{path} must be a non-negative integer")
    return value


def _decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool):
        raise OpportunityIntelligenceError(
            f"{path} must be a finite non-negative number"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OpportunityIntelligenceError(
            f"{path} must be a finite non-negative number"
        ) from None
    if not number.is_finite() or number < 0:
        raise OpportunityIntelligenceError(
            f"{path} must be a finite non-negative number"
        )
    return number


def _round4(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def _normalize_property_apn(value: Any) -> str:
    return _NON_ALNUM.sub("", str(value or "")).upper()


def _normalize_property_address(value: Any) -> str:
    text = _NON_ALNUM.sub(" ", str(value or "")).upper()
    tokens = [token for token in _WS.sub(" ", text).strip().split(" ") if token]
    return " ".join(
        _STREET_SUFFIX.get(token, _DIRECTIONAL.get(token, token))
        for token in tokens
    )


def _property_identity(
    *, county_fips: Any, parcel_apn: Any = None, raw_address: Any = None
) -> dict[str, str]:
    county = str(county_fips or "").strip()
    apn = _normalize_property_apn(parcel_apn)
    if apn:
        return {
            "kind": "parcel", "key": f"{county}|P|{apn}",
            "county_fips": county, "basis_value": apn,
        }
    address = _normalize_property_address(raw_address)
    if address:
        return {
            "kind": "address", "key": f"{county}|A|{address}",
            "county_fips": county, "basis_value": address,
        }
    return {
        "kind": "unresolved", "key": f"{county}|U|",
        "county_fips": county, "basis_value": "",
    }


def _validate_source_rollup(rollup: Any, path: str) -> None:
    if not isinstance(rollup, Mapping):
        raise OpportunityIntelligenceError(f"{path} must be an object")
    signal_count = _nonnegative_int(
        rollup.get("landed_signal_count"), f"{path}.landed_signal_count"
    )
    property_count = _nonnegative_int(
        rollup.get("property_count"), f"{path}.property_count"
    )
    unresolved = _nonnegative_int(
        rollup.get("unresolved_signal_count"), f"{path}.unresolved_signal_count"
    )
    if unresolved > signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.unresolved_signal_count cannot exceed landed_signal_count"
        )
    if signal_count and property_count > signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.property_count cannot exceed landed_signal_count"
        )
    amplification = _decimal(
        rollup.get("signal_amplification"), f"{path}.signal_amplification"
    )
    expected_amplification = _round4(signal_count, property_count)
    if amplification != expected_amplification:
        raise OpportunityIntelligenceError(
            f"{path}.signal_amplification must equal landed_signal_count / "
            f"property_count ({expected_amplification})"
        )

    kinds = rollup.get("identity_kinds")
    if not isinstance(kinds, Mapping) or not set(kinds) <= PROPERTY_IDENTITY_KINDS:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds may contain only parcel/address/unresolved"
        )
    kind_counts = {
        str(kind): _nonnegative_int(count, f"{path}.identity_kinds.{kind}")
        for kind, count in kinds.items()
    }
    if sum(kind_counts.values()) != signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds must sum to landed_signal_count"
        )
    if kind_counts.get("unresolved", 0) != unresolved:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds.unresolved must equal unresolved_signal_count"
        )

    histogram = rollup.get("multiplicity_histogram")
    if not isinstance(histogram, Mapping):
        raise OpportunityIntelligenceError(
            f"{path}.multiplicity_histogram must be an object"
        )
    property_total = 0
    resolved_signal_total = 0
    for multiplicity, count in histogram.items():
        try:
            multiplicity_int = int(str(multiplicity))
        except ValueError:
            raise OpportunityIntelligenceError(
                f"{path}.multiplicity_histogram keys must be positive integers"
            ) from None
        if multiplicity_int <= 0 or str(multiplicity_int) != str(multiplicity):
            raise OpportunityIntelligenceError(
                f"{path}.multiplicity_histogram keys must be positive integers"
            )
        property_bucket = _nonnegative_int(
            count, f"{path}.multiplicity_histogram.{multiplicity}"
        )
        property_total += property_bucket
        resolved_signal_total += multiplicity_int * property_bucket
    if property_total != property_count:
        raise OpportunityIntelligenceError(
            f"{path}.multiplicity_histogram must account for every property"
        )
    if resolved_signal_total != signal_count - unresolved:
        raise OpportunityIntelligenceError(
            f"{path}.multiplicity_histogram must account for every resolved signal"
        )


def _validate_property_population(
    population: Any, *, signals: Sequence[Mapping[str, Any]],
    subject: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    path = "input.property_population"
    if not isinstance(population, Mapping):
        raise OpportunityIntelligenceError(f"{path} must be an object")
    if population.get("schema_version") != PROPERTY_POPULATION_CONTRACT_VERSION:
        raise OpportunityIntelligenceError(
            f"{path}.schema_version must be {PROPERTY_POPULATION_CONTRACT_VERSION}"
        )
    if population.get("scope") != "SUBJECT_SIGNAL_SET":
        raise OpportunityIntelligenceError(f"{path}.scope must be SUBJECT_SIGNAL_SET")

    signal_count = _nonnegative_int(
        population.get("signal_observation_count"),
        f"{path}.signal_observation_count",
    )
    if signal_count != len(signals):
        raise OpportunityIntelligenceError(
            f"{path}.signal_observation_count must equal len(input.signals)"
        )
    property_count = _nonnegative_int(
        population.get("property_count"), f"{path}.property_count"
    )
    unresolved = _nonnegative_int(
        population.get("unresolved_signal_count"),
        f"{path}.unresolved_signal_count",
    )
    if unresolved > signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.unresolved_signal_count cannot exceed signal_observation_count"
        )

    identity = population.get("property_identity")
    if not isinstance(identity, Mapping):
        raise OpportunityIntelligenceError(f"{path}.property_identity must be an object")
    kind = identity.get("kind")
    if kind not in PROPERTY_IDENTITY_KINDS:
        raise OpportunityIntelligenceError(
            f"{path}.property_identity.kind must be parcel/address/unresolved"
        )
    county_fips = _nonempty(
        identity.get("county_fips"), f"{path}.property_identity.county_fips"
    )
    basis_value = str(identity.get("basis_value") or "")
    expected_identity = _property_identity(
        county_fips=county_fips,
        parcel_apn=basis_value if kind == "parcel" else None,
        raw_address=basis_value if kind == "address" else None,
    )
    if kind == "unresolved":
        expected_identity = _property_identity(county_fips=county_fips)
    if dict(identity) != expected_identity:
        raise OpportunityIntelligenceError(
            f"{path}.property_identity does not match the source-factory property-key rules"
        )
    if subject is not None and any(
        subject.get(field) not in (None, "")
        for field in ("county_fips", "geography", "parcel_apn", "raw_address")
    ):
        subject_identity = _property_identity(
            county_fips=subject.get("county_fips") or subject.get("geography"),
            parcel_apn=subject.get("parcel_apn"),
            raw_address=subject.get("raw_address"),
        )
        if subject_identity != expected_identity:
            raise OpportunityIntelligenceError(
                "input.subject resolves to a different property identity than "
                "input.property_population"
            )
    expected_property_count = 0 if kind == "unresolved" else 1
    if property_count != expected_property_count:
        raise OpportunityIntelligenceError(
            f"{path}.property_count must be {expected_property_count} for "
            f"a {kind} subject identity"
        )

    kinds = population.get("identity_kinds")
    if not isinstance(kinds, Mapping) or not set(kinds) <= PROPERTY_IDENTITY_KINDS:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds may contain only parcel/address/unresolved"
        )
    kind_counts = {
        str(identity_kind): _nonnegative_int(
            count, f"{path}.identity_kinds.{identity_kind}"
        )
        for identity_kind, count in kinds.items()
    }
    if sum(kind_counts.values()) != signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds must sum to signal_observation_count"
        )
    if kind_counts.get("unresolved", 0) != unresolved:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds.unresolved must equal unresolved_signal_count"
        )
    if signal_count and kind_counts.get(str(kind), 0) != signal_count:
        raise OpportunityIntelligenceError(
            f"{path}.identity_kinds must place every observation on the subject identity"
        )

    amplification = _decimal(
        population.get("signal_amplification"), f"{path}.signal_amplification"
    )
    expected_amplification = _round4(signal_count, property_count)
    if amplification != expected_amplification:
        raise OpportunityIntelligenceError(
            f"{path}.signal_amplification must equal signal_observation_count / "
            f"property_count ({expected_amplification})"
        )

    provenance = population.get("dedupe_provenance")
    if not isinstance(provenance, Mapping):
        raise OpportunityIntelligenceError(f"{path}.dedupe_provenance must be an object")
    _nonempty(
        provenance.get("identity_method"),
        f"{path}.dedupe_provenance.identity_method",
    )
    producer_contract = _nonempty(
        provenance.get("producer_contract"),
        f"{path}.dedupe_provenance.producer_contract",
    )
    if producer_contract == SOURCE_FACTORY_RUN_RECEIPT_VERSION:
        for field in (
            "run_id", "factory_version", "producer_commit", "source_rollup_sha256",
        ):
            _nonempty(provenance.get(field), f"{path}.dedupe_provenance.{field}")
        source_rollup = provenance.get("source_rollup")
        _validate_source_rollup(
            source_rollup, f"{path}.dedupe_provenance.source_rollup"
        )
        if provenance.get("source_rollup_sha256") != section_hash(source_rollup):
            raise OpportunityIntelligenceError(
                f"{path}.dedupe_provenance.source_rollup_sha256 does not bind "
                "the source_rollup"
            )

    for index, signal in enumerate(signals):
        signal_property_key = signal.get("property_key")
        if signal_property_key is not None and signal_property_key != identity["key"]:
            raise OpportunityIntelligenceError(
                f"input.signals[{index}].property_key does not match property_population"
            )
        has_identity_input = any(
            signal.get(field) not in (None, "")
            for field in ("geography", "county_fips", "parcel_apn", "raw_address")
        )
        if has_identity_input:
            observed_identity = _property_identity(
                county_fips=signal.get("geography") or signal.get("county_fips"),
                parcel_apn=signal.get("parcel_apn"),
                raw_address=signal.get("raw_address"),
            )
            if observed_identity != expected_identity:
                raise OpportunityIntelligenceError(
                    f"input.signals[{index}] resolves to a different property identity"
                )
    return population


def _recipe_digest(recipe: Mapping[str, Any]) -> str:
    body = dict(recipe)
    body.pop("config_digest", None)
    return section_hash(body)


def _validate_recipe(recipe: Any) -> Mapping[str, Any]:
    if not isinstance(recipe, Mapping):
        raise OpportunityIntelligenceError("recipe must be an object")
    if recipe.get("schema_version") != RECIPE_CONTRACT_VERSION:
        raise OpportunityIntelligenceError(
            f"recipe.schema_version must be {RECIPE_CONTRACT_VERSION}"
        )
    for field in ("recipe_id", "version", "score_version", "config_digest"):
        _nonempty(recipe.get(field), f"recipe.{field}")
    if recipe.get("score_version") != SCORE_VERSION:
        raise OpportunityIntelligenceError(
            f"unsupported score_version {recipe.get('score_version')!r}"
        )
    observed_digest = _recipe_digest(recipe)
    if recipe.get("config_digest") != observed_digest:
        raise OpportunityIntelligenceError(
            "recipe.config_digest does not match the effective recipe config"
        )

    score = recipe.get("score")
    if not isinstance(score, Mapping):
        raise OpportunityIntelligenceError("recipe.score must be an object")
    for field in ("base", "minimum", "maximum"):
        if not isinstance(score.get(field), int):
            raise OpportunityIntelligenceError(f"recipe.score.{field} must be integer")
    if not score["minimum"] <= score["base"] <= score["maximum"]:
        raise OpportunityIntelligenceError("recipe score base is outside its bounds")

    aliases = recipe.get("asset_aliases")
    if not isinstance(aliases, Mapping):
        raise OpportunityIntelligenceError("recipe.asset_aliases must be an object")
    if any(value not in ASSET_CLASSES for value in aliases.values()):
        raise OpportunityIntelligenceError("recipe.asset_aliases has unknown asset class")

    rules = _as_list(recipe.get("signal_rules"), "recipe.signal_rules")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        path = f"recipe.signal_rules[{index}]"
        if not isinstance(rule, Mapping):
            raise OpportunityIntelligenceError(f"{path} must be an object")
        signal_type = _nonempty(rule.get("signal_type"), f"{path}.signal_type")
        _nonempty(rule.get("label"), f"{path}.label")
        _nonempty(rule.get("category"), f"{path}.category")
        if signal_type in seen:
            raise OpportunityIntelligenceError(f"duplicate recipe signal {signal_type}")
        seen.add(signal_type)
        for field in ("points_if_present", "points_if_absent", "confidence_weight"):
            if not isinstance(rule.get(field), int):
                raise OpportunityIntelligenceError(f"{path}.{field} must be integer")
        if rule["confidence_weight"] <= 0:
            raise OpportunityIntelligenceError(f"{path}.confidence_weight must be > 0")
        if not isinstance(rule.get("material"), bool):
            raise OpportunityIntelligenceError(f"{path}.material must be boolean")
        for source_index, source in enumerate(
            _as_list(rule.get("next_sources", []), f"{path}.next_sources")
        ):
            if not isinstance(source, Mapping):
                raise OpportunityIntelligenceError(
                    f"{path}.next_sources[{source_index}] must be an object"
                )
            _nonempty(source.get("source_id"), "recipe source_id")
            _nonempty(
                source.get("expected_information"),
                "recipe source expected_information",
            )
            if source.get("mode") not in {"AUTOMATIC", "MANUAL"}:
                raise OpportunityIntelligenceError("recipe source mode is not recognized")

    confidence = recipe.get("confidence")
    if not isinstance(confidence, Mapping):
        raise OpportunityIntelligenceError("recipe.confidence must be an object")
    _nonempty(confidence.get("formula"), "recipe.confidence.formula")
    credits = confidence.get("credits")
    if not isinstance(credits, Mapping) or set(credits) != CONFIDENCE_LEVELS:
        raise OpportunityIntelligenceError(
            "recipe.confidence.credits must cover HIGH/MEDIUM/LOW/UNKNOWN"
        )
    for level, credit in credits.items():
        if (
            isinstance(credit, bool)
            or not isinstance(credit, (int, float))
            or not 0 <= credit <= 1
        ):
            raise OpportunityIntelligenceError(
                f"recipe.confidence.credits.{level} must be between 0 and 1"
            )
    conflicted_credit = confidence.get("conflicted_credit")
    if (
        isinstance(conflicted_credit, bool)
        or not isinstance(conflicted_credit, (int, float))
        or not 0 <= conflicted_credit <= 1
    ):
        raise OpportunityIntelligenceError(
            "recipe.confidence.conflicted_credit must be between 0 and 1"
        )
    if not isinstance(confidence.get("readiness_fact_weight"), int) or confidence[
        "readiness_fact_weight"
    ] <= 0:
        raise OpportunityIntelligenceError(
            "recipe.confidence.readiness_fact_weight must be positive integer"
        )

    readiness = recipe.get("readiness")
    if not isinstance(readiness, Mapping):
        raise OpportunityIntelligenceError("recipe.readiness must be an object")
    for axis in ("authority", "contact", "deal", "offer"):
        facts = _as_list(readiness.get(axis), f"recipe.readiness.{axis}")
        if not facts:
            raise OpportunityIntelligenceError(f"recipe.readiness.{axis} is empty")
        for fact in facts:
            _nonempty(fact, f"recipe.readiness.{axis} fact")

    action_rules = _as_list(recipe.get("action_rules"), "recipe.action_rules")
    if not action_rules:
        raise OpportunityIntelligenceError("recipe.action_rules is empty")
    seen_action_facts: set[str] = set()
    for index, action_rule in enumerate(action_rules):
        path = f"recipe.action_rules[{index}]"
        if not isinstance(action_rule, Mapping):
            raise OpportunityIntelligenceError(f"{path} must be an object")
        fact = _nonempty(action_rule.get("fact"), f"{path}.fact")
        if fact in seen_action_facts:
            raise OpportunityIntelligenceError(
                f"recipe action fact repeated: {fact}"
            )
        seen_action_facts.add(fact)
        for field in ("action", "route"):
            _nonempty(action_rule.get(field), f"{path}.{field}")
        states = _as_list(action_rule.get("when"), f"{path}.when")
        if not states or not set(states) <= READINESS_FACT_STATES:
            raise OpportunityIntelligenceError(f"{path}.when contains an unknown state")

    priority_bands = _as_list(recipe.get("priority_bands"), "recipe.priority_bands")
    if not priority_bands:
        raise OpportunityIntelligenceError("recipe.priority_bands is empty")
    for index, band in enumerate(priority_bands):
        path = f"recipe.priority_bands[{index}]"
        if not isinstance(band, Mapping):
            raise OpportunityIntelligenceError(f"{path} must be an object")
        for field in ("min_score", "min_confidence"):
            if not isinstance(band.get(field), int) or not 0 <= band[field] <= 100:
                raise OpportunityIntelligenceError(
                    f"{path}.{field} must be an integer from 0 through 100"
                )
        _nonempty(band.get("priority"), f"{path}.priority")

    fact_sources = recipe.get("fact_sources")
    if not isinstance(fact_sources, Mapping):
        raise OpportunityIntelligenceError("recipe.fact_sources must be an object")
    for fact, sources in fact_sources.items():
        _nonempty(fact, "recipe.fact_sources key")
        for index, source in enumerate(_as_list(sources, f"recipe.fact_sources.{fact}")):
            path = f"recipe.fact_sources.{fact}[{index}]"
            if not isinstance(source, Mapping):
                raise OpportunityIntelligenceError(f"{path} must be an object")
            _nonempty(source.get("source_id"), f"{path}.source_id")
            _nonempty(
                source.get("expected_information"),
                f"{path}.expected_information",
            )
            if source.get("mode") not in {"AUTOMATIC", "MANUAL"}:
                raise OpportunityIntelligenceError(f"{path}.mode is not recognized")
    return recipe


def _validate_payload(payload: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise OpportunityIntelligenceError("input must be an object")
    if payload.get("schema_version") != INPUT_CONTRACT_VERSION:
        raise OpportunityIntelligenceError(
            f"input.schema_version must be {INPUT_CONTRACT_VERSION}"
        )
    for field in (
        "as_of", "recipe", "subject", "asset", "signals",
        "property_population", "readiness", "research_attempts", "lineage",
    ):
        if field not in payload:
            raise OpportunityIntelligenceError(f"input.{field} is required")
    _nonempty(payload.get("as_of"), "input.as_of")
    subject = payload.get("subject")
    if not isinstance(subject, Mapping):
        raise OpportunityIntelligenceError("input.subject must be an object")
    _nonempty(subject.get("subject_id"), "input.subject.subject_id")
    recipe = _validate_recipe(payload.get("recipe"))

    if not isinstance(payload.get("asset", {}), Mapping):
        raise OpportunityIntelligenceError("input.asset must be an object")
    signals = _as_list(payload.get("signals", []), "input.signals")
    signal_ids: set[str] = set()
    for index, signal in enumerate(signals):
        path = f"input.signals[{index}]"
        if not isinstance(signal, Mapping):
            raise OpportunityIntelligenceError(f"{path} must be an object")
        signal_id = _nonempty(signal.get("signal_id"), f"{path}.signal_id")
        if signal_id in signal_ids:
            raise OpportunityIntelligenceError(f"duplicate signal_id {signal_id!r}")
        signal_ids.add(signal_id)
        _nonempty(signal.get("signal_type"), f"{path}.signal_type")
        if signal.get("state") not in SIGNAL_STATES:
            raise OpportunityIntelligenceError(f"{path}.state is not recognized")
        if signal.get("confidence") not in CONFIDENCE_LEVELS:
            raise OpportunityIntelligenceError(f"{path}.confidence is not recognized")
        for field in ("evidence_refs", "source_families"):
            for value in _as_list(signal.get(field, []), f"{path}.{field}"):
                _nonempty(value, f"{path}.{field} item")

    _validate_property_population(
        payload.get("property_population"), signals=signals, subject=subject
    )

    readiness = payload.get("readiness", {})
    if not isinstance(readiness, Mapping):
        raise OpportunityIntelligenceError("input.readiness must be an object")
    for fact, state in readiness.items():
        _nonempty(fact, "input.readiness key")
        if state not in READINESS_FACT_STATES:
            raise OpportunityIntelligenceError(
                f"input.readiness.{fact} must be YES, NO, or UNKNOWN"
            )

    for index, attempt in enumerate(
        _as_list(payload.get("research_attempts", []), "input.research_attempts")
    ):
        path = f"input.research_attempts[{index}]"
        if not isinstance(attempt, Mapping):
            raise OpportunityIntelligenceError(f"{path} must be an object")
        _nonempty(attempt.get("source_id"), f"{path}.source_id")
        state = _nonempty(attempt.get("result_state"), f"{path}.result_state")
        if generic_result_state(state) is None:
            raise OpportunityIntelligenceError(
                f"{path}.result_state {state!r} is not in the T50 contract"
            )
        if "evidence_refs" in attempt:
            for value in _as_list(attempt["evidence_refs"], f"{path}.evidence_refs"):
                _nonempty(value, f"{path}.evidence_refs item")

    lineage = payload.get("lineage", {})
    if not isinstance(lineage, Mapping):
        raise OpportunityIntelligenceError("input.lineage must be an object")
    for field in LINEAGE_LIST_FIELDS:
        for value in _as_list(lineage.get(field, []), f"input.lineage.{field}"):
            _nonempty(value, f"input.lineage.{field} item")
    for field, values in lineage.items():
        if field in LINEAGE_LIST_FIELDS:
            continue
        for value in _as_list(values, f"input.lineage.{field}"):
            _nonempty(value, f"input.lineage.{field} item")

    if "extensions" in payload and not isinstance(payload["extensions"], Mapping):
        raise OpportunityIntelligenceError("input.extensions must be an object")
    if "resolution_context" in payload and not isinstance(
        payload["resolution_context"], Mapping
    ):
        raise OpportunityIntelligenceError(
            "input.resolution_context must be an object"
        )
    return payload, recipe


def _norm_token(value: Any) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", " ").split())


def _classify_asset(asset: Mapping[str, Any], recipe: Mapping[str, Any]) -> dict[str, Any]:
    aliases = recipe["asset_aliases"]
    candidates: list[dict[str, str]] = []
    for field in ("declared_class", "asset_class", "property_type", "land_use"):
        raw = asset.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        normalized = _norm_token(raw)
        classification = aliases.get(normalized)
        if classification:
            candidates.append(
                {"field": field, "raw": str(raw), "normalized": normalized,
                 "classification": classification}
            )

    known = {c["classification"] for c in candidates if c["classification"] != "unknown"}
    if len(known) > 1:
        return {
            "asset_class": "unknown",
            "state": "CONFLICTED",
            "why": f"Asset inputs disagree across canonical classes: {sorted(known)}.",
            "inputs_used": candidates,
            "raw_facts": deepcopy(dict(asset)),
            "what_would_resolve": "Adjudicate the class from an authoritative parcel/land-use source.",
        }
    if len(known) == 1:
        classification = next(iter(known))
        used = [c for c in candidates if c["classification"] == classification]
        return {
            "asset_class": classification,
            "state": "KNOWN",
            "why": (
                f"Exact recipe alias mapped {used[0]['field']}={used[0]['raw']!r} "
                f"to {classification}."
            ),
            "inputs_used": candidates,
            "raw_facts": deepcopy(dict(asset)),
            "what_would_resolve": None,
        }
    return {
        "asset_class": "unknown",
        "state": "UNKNOWN",
        "why": "No supplied asset fact matched an exact versioned recipe alias.",
        "inputs_used": candidates,
        "raw_facts": deepcopy(dict(asset)),
        "what_would_resolve": "Supply an authoritative canonical asset class or exact land-use value.",
    }


_CONFIDENCE_ORDER = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _union_strings(observations: Sequence[Mapping[str, Any]], field: str) -> list[str]:
    return sorted({str(value) for item in observations for value in item.get(field, [])})


def _aggregate_rule(
    rule: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    states = {str(item["state"]) for item in observations}
    known_states = states - {"UNKNOWN"}
    if "CONFLICTED" in states or {"PRESENT", "ABSENT"} <= known_states:
        state = "CONFLICTED"
        relevant = list(observations)
        reason = "Observations conflict; no opportunity-score delta is applied."
    elif "PRESENT" in known_states:
        state = "PRESENT"
        relevant = [item for item in observations if item["state"] == "PRESENT"]
        reason = f"{len(relevant)} observation(s) state the factor is present."
    elif "ABSENT" in known_states:
        state = "ABSENT"
        relevant = [item for item in observations if item["state"] == "ABSENT"]
        reason = f"{len(relevant)} observation(s) state the factor is absent."
    else:
        state = "UNKNOWN"
        relevant = list(observations)
        reason = (
            "No observation supplied." if not observations
            else "All supplied observations explicitly state UNKNOWN."
        )

    if relevant and state not in {"UNKNOWN", "CONFLICTED"}:
        confidence = min(
            (str(item["confidence"]) for item in relevant),
            key=lambda value: _CONFIDENCE_ORDER[value],
        )
        confidence_reason = "Conservative minimum stated confidence across consistent observations."
    elif state == "CONFLICTED":
        confidence = "UNKNOWN"
        confidence_reason = "A conflict has no resolved confidence tier."
    else:
        confidence = "UNKNOWN"
        confidence_reason = "No resolved evidence confidence is available."

    if state == "PRESENT":
        delta = int(rule["points_if_present"])
    elif state == "ABSENT":
        delta = int(rule["points_if_absent"])
    else:
        delta = 0
    return {
        "factor_id": rule["signal_type"],
        "label": rule.get("label") or rule["signal_type"],
        "category": rule.get("category") or "uncategorized",
        "state": state,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "delta": delta,
        "points_if_present": int(rule["points_if_present"]),
        "points_if_absent": int(rule["points_if_absent"]),
        "material": bool(rule["material"]),
        "confidence_weight": int(rule["confidence_weight"]),
        "signal_ids": sorted(str(item["signal_id"]) for item in observations),
        "evidence_refs": _union_strings(observations, "evidence_refs"),
        "source_families": _union_strings(observations, "source_families"),
        "why": reason,
    }


def _score_signals(
    signals: Sequence[Mapping[str, Any]], recipe: Mapping[str, Any]
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for signal in signals:
        by_type[str(signal["signal_type"])].append(signal)

    rules = list(recipe["signal_rules"])
    known_rule_types = {str(rule["signal_type"]) for rule in rules}
    factors = [_aggregate_rule(rule, by_type.get(str(rule["signal_type"]), []))
               for rule in rules]
    unrecognized = [deepcopy(dict(signal)) for signal in signals
                    if str(signal["signal_type"]) not in known_rule_types]

    raw_score = int(recipe["score"]["base"]) + sum(factor["delta"] for factor in factors)
    score = max(int(recipe["score"]["minimum"]),
                min(int(recipe["score"]["maximum"]), raw_score))

    positives = [
        {key: factor[key] for key in (
            "factor_id", "label", "state", "delta", "why", "evidence_refs",
        )}
        for factor in factors if factor["delta"] > 0
    ]
    negatives = [
        {key: factor[key] for key in (
            "factor_id", "label", "state", "delta", "why", "evidence_refs",
        )}
        for factor in factors if factor["delta"] < 0
    ]
    unknowns: list[dict[str, Any]] = []
    for factor in factors:
        if factor["material"] and factor["state"] in {"UNKNOWN", "CONFLICTED"}:
            possible = sorted({factor["points_if_present"], factor["points_if_absent"]})
            unknowns.append({
                "factor_id": factor["factor_id"],
                "label": factor["label"],
                "state": factor["state"],
                "possible_delta": {"minimum": possible[0], "maximum": possible[-1]},
                "why_unknown": factor["why"],
                "what_would_resolve": [
                    dict(source) for source in next(
                        rule["next_sources"] for rule in rules
                        if rule["signal_type"] == factor["factor_id"]
                    )
                ],
            })
    for signal in unrecognized:
        unknowns.append({
            "factor_id": str(signal["signal_type"]),
            "label": str(signal["signal_type"]),
            "state": "UNKNOWN",
            "possible_delta": {"minimum": None, "maximum": None},
            "why_unknown": "The recipe has no rule for this signal type; it was preserved but not scored.",
            "what_would_resolve": [
                {"source_id": "RECIPE_REVIEW", "mode": "MANUAL",
                 "expected_information": "a versioned rule or an explicit decision to remain unscored"}
            ],
        })

    stack = {
        "observation_count": len(signals),
        "count_kind": "SIGNAL_OBSERVATIONS_NOT_PROPERTIES",
        "distinct_signal_types": sorted(by_type),
        "scored_rule_count": len(factors),
        "present_factor_count": sum(f["state"] == "PRESENT" for f in factors),
        "conflicted_factor_count": sum(f["state"] == "CONFLICTED" for f in factors),
        "raw_observations": deepcopy(list(signals)),
        "normalized_factors": factors,
        "unrecognized_observations": unrecognized,
    }
    score_detail = {
        "score_version": recipe["score_version"],
        "formula": "clamp(recipe.base + sum(named factor deltas), minimum, maximum)",
        "base_score": int(recipe["score"]["base"]),
        "raw_score": raw_score,
        "score": score,
        "minimum": int(recipe["score"]["minimum"]),
        "maximum": int(recipe["score"]["maximum"]),
        "components": [
            {key: factor[key] for key in (
                "factor_id", "state", "delta", "points_if_present",
                "points_if_absent", "signal_ids", "evidence_refs", "why",
            )}
            for factor in factors
        ],
    }
    return score_detail, positives, negatives, unknowns, stack


def _required_facts(recipe: Mapping[str, Any]) -> tuple[str, ...]:
    facts = {
        str(fact)
        for axis in recipe["readiness"].values()
        for fact in axis
    }
    facts.update(str(rule["fact"]) for rule in recipe["action_rules"])
    return tuple(sorted(facts))


def _axis_readiness(axis: str, facts: Sequence[str], states: Mapping[str, str]) -> dict[str, Any]:
    no = [fact for fact in facts if states[fact] == "NO"]
    unknown = [fact for fact in facts if states[fact] == "UNKNOWN"]
    if no:
        state = "NOT_READY"
        why = f"{axis} is not ready because known blockers are present: {', '.join(no)}."
    elif unknown:
        state = "UNKNOWN"
        why = f"{axis} cannot be declared ready until these facts are known: {', '.join(unknown)}."
    else:
        state = "READY"
        why = f"Every versioned {axis} requirement is YES."
    return {
        "state": state,
        "requirements": list(facts),
        "fact_states": {fact: states[fact] for fact in facts},
        "blocking_facts": no,
        "unknown_facts": unknown,
        "why": why,
    }


def _readiness(payload: Mapping[str, Any], recipe: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    supplied = payload.get("readiness", {})
    facts = {fact: str(supplied.get(fact, "UNKNOWN")) for fact in _required_facts(recipe)}
    axes = {
        axis: _axis_readiness(axis, list(recipe["readiness"][axis]), facts)
        for axis in ("authority", "contact", "deal", "offer")
    }
    seller_facts = list(dict.fromkeys(
        list(recipe["readiness"]["authority"]) + list(recipe["readiness"]["contact"])
    ))
    axes["seller"] = _axis_readiness("seller", seller_facts, facts)
    return {
        "facts": facts,
        "supplied_facts": deepcopy(dict(supplied)),
        "authority_readiness": axes["authority"],
        "contact_readiness": axes["contact"],
        "deal_readiness": axes["deal"],
        "offer_readiness": axes["offer"],
        "seller_readiness": axes["seller"],
        "system_may_contact": axes["seller"]["state"] == "READY",
        "system_may_make_offer": axes["offer"]["state"] == "READY",
        "authorization_note": (
            "A READY result permits an authorized downstream operator workflow only; "
            "this evaluator never contacts a seller or sends an offer."
        ),
    }, facts


def _confidence(
    stack: Mapping[str, Any], facts: Mapping[str, str], recipe: Mapping[str, Any]
) -> dict[str, Any]:
    confidence_recipe = recipe["confidence"]
    credits = {key: Decimal(str(value)) for key, value in confidence_recipe["credits"].items()}
    conflict_credit = Decimal(str(confidence_recipe.get("conflicted_credit", 0)))
    components: list[dict[str, Any]] = []
    possible = Decimal("0")
    earned = Decimal("0")
    for factor in stack["normalized_factors"]:
        if not factor["material"]:
            continue
        weight = Decimal(str(factor["confidence_weight"]))
        if factor["state"] == "CONFLICTED":
            credit = conflict_credit
        elif factor["state"] == "UNKNOWN":
            credit = Decimal("0")
        else:
            credit = credits[factor["confidence"]]
        component_earned = weight * credit
        possible += weight
        earned += component_earned
        components.append({
            "factor_id": factor["factor_id"], "kind": "signal",
            "state": factor["state"], "confidence": factor["confidence"],
            "weight": str(weight), "credit": str(credit),
            "earned_weight": str(component_earned),
        })

    readiness_weight = Decimal(str(confidence_recipe["readiness_fact_weight"]))
    for fact in sorted(facts):
        state = facts[fact]
        credit = Decimal("0") if state == "UNKNOWN" else Decimal("1")
        component_earned = readiness_weight * credit
        possible += readiness_weight
        earned += component_earned
        components.append({
            "factor_id": fact, "kind": "readiness", "state": state,
            "confidence": "KNOWN" if state != "UNKNOWN" else "UNKNOWN",
            "weight": str(readiness_weight), "credit": str(credit),
            "earned_weight": str(component_earned),
        })
    score = 0 if possible == 0 else int(
        ((earned / possible) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return {
        "score": score,
        "formula": confidence_recipe["formula"],
        "earned_weight": str(earned),
        "possible_weight": str(possible),
        "components": components,
        "unknown_factors": [
            c["factor_id"] for c in components if Decimal(c["credit"]) == 0
        ],
    }


def _research_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    exhausted: set[str] = set()
    blocked: set[str] = set()
    operator_required: set[str] = set()
    for attempt in attempts:
        original = str(attempt["result_state"])
        state = generic_result_state(original)
        if state is None:  # validation should make this unreachable; fail closed anyway.
            raise OpportunityIntelligenceError(f"unrecognized T50 result {original!r}")
        source_id = str(attempt["source_id"])
        if supports_replay_suppression_generic(original):
            exhausted.add(source_id)
        if state == "BLOCKED":
            blocked.add(source_id)
        if state == "OPERATOR_REQUIRED":
            operator_required.add(source_id)
        normalized.append({
            "attempt_id": attempt.get("attempt_id"),
            "source_id": source_id,
            "original_state": original,
            "generic_state": state,
            "is_evidential": is_evidential(original),
            "is_positive": is_positive(original),
            "suppresses_replay": supports_replay_suppression_generic(original),
            "evidence_refs": deepcopy(list(attempt.get("evidence_refs", []))),
        })
    return {
        "contract_version": RESEARCH_RESULT_CONTRACT_VERSION,
        "raw_attempts": deepcopy(list(attempts)),
        "normalized_attempts": normalized,
        "exhausted_source_ids": sorted(exhausted),
        "blocked_source_ids": sorted(blocked),
        "operator_required_source_ids": sorted(operator_required),
    }


def _next_action(
    facts: Mapping[str, str], stack: Mapping[str, Any], recipe: Mapping[str, Any]
) -> dict[str, Any]:
    government = next(
        (factor for factor in stack["normalized_factors"]
         if factor["factor_id"] == "government_owner"),
        None,
    )
    if government and government["state"] == "PRESENT":
        return {
            "action": "HOLD_NOT_A_SELLER",
            "route": "DO_NOT_ROUTE",
            "trigger_fact": "government_owner",
            "trigger_state": "PRESENT",
            "why": "Government ownership is an explicit exclusion in this recipe.",
            "execution_authority": "NONE",
        }
    for rule in recipe["action_rules"]:
        fact = str(rule["fact"])
        if facts.get(fact, "UNKNOWN") in set(rule["when"]):
            state = facts.get(fact, "UNKNOWN")
            return {
                "action": str(rule["action"]),
                "route": str(rule["route"]),
                "trigger_fact": fact,
                "trigger_state": state,
                "why": f"First unresolved recipe gate is {fact}={state}.",
                "execution_authority": "NONE",
            }
    return {
        "action": "OPERATOR_REVIEW_AUTHORIZED_OPPORTUNITY",
        "route": "ACQUISITION_REVIEW",
        "trigger_fact": None,
        "trigger_state": None,
        "why": "All versioned readiness facts are YES; an operator still owns execution.",
        "execution_authority": "NONE",
    }


_RESOLUTION_CONTEXT_KEYS = {
    "owner_class", "owner_condition", "contact_resolution_state", "county",
    "has_living_identity", "has_high_phone", "tier_c_held", "tier_c_certified",
    "attempted_sources", "entity_resolution_state", "entity_principal_known",
    "estate_public_index_searched",
}


def _recipe_source(
    action: Mapping[str, Any], unknowns: Sequence[Mapping[str, Any]],
    research: Mapping[str, Any], recipe: Mapping[str, Any],
) -> dict[str, Any]:
    sources: list[Mapping[str, Any]] = []
    fact = action.get("trigger_fact")
    if fact:
        sources.extend(recipe["fact_sources"].get(fact, []))
    if not sources:
        for unknown in unknowns:
            sources.extend(unknown.get("what_would_resolve", []))
            if sources:
                break

    exhausted = set(research["exhausted_source_ids"])
    blocked = set(research["blocked_source_ids"])
    operator_required = set(research["operator_required_source_ids"])
    skipped: list[dict[str, str]] = []
    for source in sources:
        source_id = str(source["source_id"])
        if source_id in exhausted:
            skipped.append({"source_id": source_id, "reason": "evidential no-match/not-applicable already recorded"})
            continue
        if source_id in blocked:
            skipped.append({"source_id": source_id, "reason": "source instrument recorded BLOCKED"})
            continue
        mode = "MANUAL" if source_id in operator_required else str(source.get("mode", "MANUAL"))
        return {
            "source_id": source_id,
            "mode": mode,
            "expected_information": source.get("expected_information"),
            "why": (
                f"This is the first non-exhausted source for {fact or 'the first material unknown'}."
            ),
            "origin": f"{recipe['recipe_id']}@{recipe['version']}",
            "skipped_sources": skipped,
        }
    return {
        "source_id": "NONE_REMAINING_IN_RECIPE",
        "mode": "MANUAL",
        "expected_information": None,
        "why": "Every declared source for the next unresolved factor is exhausted or blocked.",
        "origin": f"{recipe['recipe_id']}@{recipe['version']}",
        "skipped_sources": skipped,
    }


def _next_source(
    payload: Mapping[str, Any], action: Mapping[str, Any],
    unknowns: Sequence[Mapping[str, Any]], research: Mapping[str, Any],
    recipe: Mapping[str, Any],
) -> dict[str, Any]:
    if action.get("route") == "DO_NOT_ROUTE":
        return {
            "source_id": "NONE",
            "mode": "NONE",
            "expected_information": None,
            "why": "An explicit exclusion forbids further opportunity research routing.",
            "origin": f"{recipe['recipe_id']}@{recipe['version']}",
            "skipped_sources": [],
        }
    context = payload.get("resolution_context")
    if (
        isinstance(context, Mapping)
        and action["action"] in {"RESOLVE_IDENTITY", "ESTABLISH_LEGAL_AUTHORITY", "RESOLVE_CONTACT"}
        and all(key in context for key in ("owner_class", "owner_condition", "contact_resolution_state"))
    ):
        kwargs = {key: context[key] for key in _RESOLUTION_CONTEXT_KEYS if key in context}
        attempted = set(kwargs.get("attempted_sources") or ())
        attempted.update(research["exhausted_source_ids"])
        kwargs["attempted_sources"] = frozenset(attempted)
        route = resolution_next_source(**kwargs).to_dict()
        return {
            "source_id": route["next_source"],
            "mode": route["manual_or_automatic"],
            "expected_information": route["expected_information"],
            "why": route["why"],
            "origin": ROUTER_VERSION,
            "required_input": route["required_input"],
            "blocking_reason": route["blocking_reason"],
            "operator_queue": route["queue"],
            "cost_usd": route["cost_usd"],
            "skipped_sources": [],
        }
    return _recipe_source(action, unknowns, research, recipe)


def _priority(
    score: int, confidence: int, action: Mapping[str, Any],
    stack: Mapping[str, Any], recipe: Mapping[str, Any],
) -> dict[str, Any]:
    government = next(
        (factor for factor in stack["normalized_factors"]
         if factor["factor_id"] == "government_owner"), None,
    )
    if government and government["state"] == "PRESENT":
        return {
            "priority": "DO_NOT_ROUTE", "score": score, "confidence": confidence,
            "why": "The explicit government-owner exclusion outranks score bands.",
        }
    for band in recipe["priority_bands"]:
        if score >= int(band["min_score"]) and confidence >= int(band["min_confidence"]):
            return {
                "priority": str(band["priority"]), "score": score,
                "confidence": confidence,
                "why": (
                    f"score {score}>={band['min_score']} and confidence "
                    f"{confidence}>={band['min_confidence']}; route is {action['route']}."
                ),
            }
    raise OpportunityIntelligenceError("recipe priority bands do not cover this result")


_CORE_INPUT_FIELDS = {
    "schema_version", "as_of", "recipe", "subject", "asset", "signals",
    "property_population", "readiness", "research_attempts", "lineage",
}


def _input_extensions(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(value)
        for key, value in payload.items()
        if key not in _CORE_INPUT_FIELDS
    }


def _genome(payload: Mapping[str, Any], recipe: Mapping[str, Any]) -> dict[str, Any]:
    lineage = deepcopy(dict(payload.get("lineage", {})))
    input_extensions = _input_extensions(payload)
    return {
        "schema_version": GENOME_CONTRACT_VERSION,
        "subject": deepcopy(dict(payload["subject"])),
        "asset_facts": deepcopy(dict(payload.get("asset", {}))),
        "raw_signals": deepcopy(list(payload.get("signals", []))),
        "property_population": deepcopy(dict(payload["property_population"])),
        "raw_research_attempts": deepcopy(list(payload.get("research_attempts", []))),
        "raw_readiness_facts": deepcopy(dict(payload.get("readiness", {}))),
        "lineage": lineage,
        "lineage_counts": {
            field: len(lineage.get(field, [])) for field in LINEAGE_LIST_FIELDS
        },
        "caller_top_level_extensions": input_extensions,
        "extensions": deepcopy(dict(payload.get("extensions", {}))),
        "recipe_ref": {
            "recipe_id": recipe["recipe_id"], "version": recipe["version"],
            "config_digest": recipe["config_digest"],
            "score_version": recipe["score_version"],
        },
        "preservation": {
            "lossless_fields": [
                "subject", "asset", "signals", "property_population",
                "research_attempts", "readiness", "lineage", "extensions",
                "caller top-level extension fields",
            ],
            "note": "Raw caller payloads are copied; normalization is emitted beside them, never over them.",
        },
    }


def evaluate_opportunity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one portable opportunity input and return a deterministic receipt."""
    payload, recipe = _validate_payload(payload)
    asset = _classify_asset(payload.get("asset", {}), recipe)
    score_detail, positives, negatives, unknowns, stack = _score_signals(
        payload.get("signals", []), recipe
    )
    readiness, facts = _readiness(payload, recipe)
    confidence = _confidence(stack, facts, recipe)
    research = _research_summary(payload.get("research_attempts", []))
    action = _next_action(facts, stack, recipe)
    if action["route"] == "DO_NOT_ROUTE":
        readiness["system_may_contact"] = False
        readiness["system_may_make_offer"] = False
        readiness["exclusion_override"] = {
            "active": True,
            "factor_id": action["trigger_fact"],
            "why": "An exclusion overrides otherwise-ready contact and offer gates.",
        }
    source = _next_source(payload, action, unknowns, research, recipe)
    priority = _priority(score_detail["score"], confidence["score"], action, stack, recipe)
    genome = _genome(payload, recipe)
    genome_digest = section_hash(genome)

    what_changes = [
        {
            "factor_id": item["factor_id"],
            "current_state": item["state"],
            "possible_delta": item["possible_delta"],
            "required_sources": item["what_would_resolve"],
        }
        for item in unknowns
    ]
    material_unknowns = deepcopy(unknowns)
    if asset["state"] != "KNOWN":
        material_unknowns.append({
            "factor_id": "asset_class",
            "label": "canonical asset class",
            "state": asset["state"],
            "possible_delta": {"minimum": 0, "maximum": 0},
            "why_unknown": asset["why"],
            "what_would_resolve": [{
                "source_id": "AUTHORITATIVE_PARCEL_CLASS",
                "mode": "AUTOMATIC",
                "expected_information": "canonical parcel or land-use class",
            }],
            "effect": "Classification and routing context; the v1 score has no asset-class delta.",
        })
    for fact in sorted(fact for fact, state in facts.items() if state == "UNKNOWN"):
        readiness_unknown = {
            "factor_id": fact,
            "current_state": "UNKNOWN",
            "possible_delta": {"minimum": 0, "maximum": 0},
            "required_sources": deepcopy(list(recipe["fact_sources"].get(fact, []))),
            "effect": "Resolving this changes readiness/confidence, not the opportunity score.",
        }
        what_changes.append(readiness_unknown)
        material_unknowns.append({
            "factor_id": fact,
            "label": fact,
            "state": "UNKNOWN",
            "possible_delta": {"minimum": 0, "maximum": 0},
            "why_unknown": "The readiness fact was omitted or explicitly UNKNOWN.",
            "what_would_resolve": readiness_unknown["required_sources"],
            "effect": readiness_unknown["effect"],
        })

    result: dict[str, Any] = {
        "schema_version": RESULT_CONTRACT_VERSION,
        "evaluated_as_of": payload["as_of"],
        "subject_id": payload["subject"]["subject_id"],
        "recipe": {
            "schema_version": RECIPE_CONTRACT_VERSION,
            "recipe_id": recipe["recipe_id"],
            "version": recipe["version"],
            "config_digest": recipe["config_digest"],
            "score_version": recipe["score_version"],
        },
        "opportunity_genome": genome,
        "caller_top_level_extensions": deepcopy(
            genome["caller_top_level_extensions"]
        ),
        "genome_digest": genome_digest,
        "asset_classification": asset,
        "stacked_signals": stack,
        "property_population": deepcopy(dict(payload["property_population"])),
        "opportunity_score": score_detail,
        "confidence_score": confidence,
        "readiness": readiness,
        "research_results": research,
        "next_best_source": source,
        "next_best_action": action,
        "priority": priority,
        "routing": {
            "route": action["route"],
            "priority": priority["priority"],
            "operator_queue": source.get("operator_queue"),
            "why": f"{action['why']} {priority['why']}",
        },
        "explanation": {
            "why": [
                asset["why"],
                (
                    f"{payload['property_population']['signal_observation_count']} raw signal "
                    f"observation(s) resolve to "
                    f"{payload['property_population']['property_count']} deduped property "
                    "identity; these cardinalities are not interchangeable."
                ),
                f"Opportunity score is {score_detail['score']} from named deltas only.",
                f"Confidence is {confidence['score']} from declared coverage weights.",
                action["why"],
            ],
            "positive_factors": positives,
            "negative_factors": negatives,
            "unknown_material_factors": material_unknowns,
            "what_would_change": what_changes,
        },
        "safety": {
            "opaque_ml": False,
            "seller_contact_performed": False,
            "offer_sent_or_made": False,
            "execution_authority_granted": False,
        },
    }
    result_digest = section_hash(result)
    receipt_body = {
        "schema_version": RECEIPT_CONTRACT_VERSION,
        "input_digest": section_hash(payload),
        "recipe_digest": recipe["config_digest"],
        "genome_digest": genome_digest,
        "result_digest": result_digest,
    }
    result["receipt"] = {
        **receipt_body,
        "receipt_sha256": section_hash(receipt_body),
    }
    return result


def verify_opportunity_receipt(
    result: Mapping[str, Any], input_payload: Mapping[str, Any] | None = None
) -> bool:
    """Verify every receipt binding, including the caller-supplied input.

    The input is required because a result alone cannot prove what payload its
    claimed ``input_digest`` was calculated from.
    """
    if not isinstance(input_payload, Mapping):
        return False
    receipt = result.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    if receipt.get("schema_version") != RECEIPT_CONTRACT_VERSION:
        return False
    if receipt.get("input_digest") != section_hash(input_payload):
        return False
    body = deepcopy(dict(result))
    body.pop("receipt", None)
    if receipt.get("result_digest") != section_hash(body):
        return False
    if receipt.get("genome_digest") != section_hash(body.get("opportunity_genome")):
        return False
    if body.get("genome_digest") != receipt.get("genome_digest"):
        return False
    if receipt.get("recipe_digest") != body.get("recipe", {}).get("config_digest"):
        return False
    receipt_body = dict(receipt)
    claimed = receipt_body.pop("receipt_sha256", None)
    return claimed == section_hash(receipt_body)
