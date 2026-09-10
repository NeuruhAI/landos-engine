"""CODE_VACANCY unlock: bind existing NC tags so recipes can query them.

Gap class: RECIPE_BINDING_MISSING.
Unlock mode: PROMOTION_THEN_ACQUISITION_FREE.

The tags already exist on the NC DPS estate/ingest writers. This module does
not invent tags and does not scrape. It promotes those tagged facts onto a
queryable surface the distress / Logan / composite recipes already understand.

Closed census (do not add strings here unless a writer already emits them):

    nc_dps_writer.signal_tags
        nc_dps:{trigger}  →  nc_dps:vacancy_or_code
        (trigger from nc_dps_contract.TRIGGER_CLASSES / wave_a pipeline)

    nc_dps_raw_writer
        nc_dps:{source_type}  →  nc_dps:vacancy, nc_dps:code_violation
        (source_type in RAW_OPPORTUNITY_TYPES)

    T8 estate path (named signal, not a tag)
        CODE_ENFORCEMENT_CASE  (Charlotte HNS / app.free_routes.t8_open_data)
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .classes import (
    FAMILY_CODE_VACANCY,
    INGREDIENT_CODE_OR_VACANCY,
    INGREDIENT_CODE_VIOLATION,
    INGREDIENT_VACANCY,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)

# Exact tag strings already written into raw_meta.signal_tags.
TAG_NC_VACANCY_OR_CODE = "nc_dps:vacancy_or_code"
TAG_NC_VACANCY = "nc_dps:vacancy"
TAG_NC_CODE_VIOLATION = "nc_dps:code_violation"

EXISTING_CODE_VACANCY_TAGS = frozenset({
    TAG_NC_VACANCY_OR_CODE,
    TAG_NC_VACANCY,
    TAG_NC_CODE_VIOLATION,
})

# Existing recipe tokens (Logan / scoring / composite). Not new tags.
TOKEN_CODE_VIOLATION = INGREDIENT_CODE_VIOLATION
TOKEN_VACANCY = INGREDIENT_VACANCY

# Existing ingest source_types / trigger class / estate-path signal name.
SOURCE_TYPE_VACANCY = "vacancy"
SOURCE_TYPE_CODE_VIOLATION = "code_violation"
TRIGGER_VACANCY_OR_CODE = "vacancy_or_code"
ESTATE_SIGNAL_CODE_ENFORCEMENT_CASE = "CODE_ENFORCEMENT_CASE"

# Existing tag → existing recipe token(s). Combined trigger is one tagged fact
# that already means vacancy OR code; recipes get both existing tokens.
TAG_TO_TOKENS: dict[str, frozenset[str]] = {
    TAG_NC_CODE_VIOLATION: frozenset({TOKEN_CODE_VIOLATION}),
    TAG_NC_VACANCY: frozenset({TOKEN_VACANCY}),
    TAG_NC_VACANCY_OR_CODE: frozenset({TOKEN_CODE_VIOLATION, TOKEN_VACANCY}),
}

# Existing non-tag facts on the same ingest/estate paths → same tokens.
FACT_TO_TOKENS: dict[str, frozenset[str]] = {
    SOURCE_TYPE_CODE_VIOLATION: frozenset({TOKEN_CODE_VIOLATION}),
    SOURCE_TYPE_VACANCY: frozenset({TOKEN_VACANCY}),
    TRIGGER_VACANCY_OR_CODE: frozenset({TOKEN_CODE_VIOLATION, TOKEN_VACANCY}),
    ESTATE_SIGNAL_CODE_ENFORCEMENT_CASE: frozenset({TOKEN_CODE_VIOLATION}),
}

# Recipe-core signal_tags bind: existing NC tags only (no invented strings).
RECIPE_DIM_TAGS: dict[str, tuple[str, ...]] = {
    TOKEN_CODE_VIOLATION: (TAG_NC_CODE_VIOLATION, TAG_NC_VACANCY_OR_CODE),
    TOKEN_VACANCY: (TAG_NC_VACANCY, TAG_NC_VACANCY_OR_CODE),
}

# Logan FACTOR_SIGNALS extras: existing tags + the T8 signal name.
LOGAN_CODE_VIOLATION_EVIDENCE: frozenset[str] = frozenset({
    TAG_NC_CODE_VIOLATION,
    TAG_NC_VACANCY_OR_CODE,
    ESTATE_SIGNAL_CODE_ENFORCEMENT_CASE,
})
LOGAN_VACANT_BOARDED_EVIDENCE: frozenset[str] = frozenset({
    TAG_NC_VACANCY,
    TAG_NC_VACANCY_OR_CODE,
})

TAG_CENSUS: tuple[dict[str, str], ...] = (
    {
        "tag": TAG_NC_VACANCY_OR_CODE,
        "writer": "app/nc_dps_writer.py:signal_tags",
        "when": "scores.trigger_classes contains vacancy_or_code",
        "path": "estate/ingest (qualified NC DPS)",
    },
    {
        "tag": TAG_NC_VACANCY,
        "writer": "app/nc_dps_raw_writer.py",
        "when": "source_type == vacancy",
        "path": "estate/ingest (raw NC DPS)",
    },
    {
        "tag": TAG_NC_CODE_VIOLATION,
        "writer": "app/nc_dps_raw_writer.py",
        "when": "source_type == code_violation",
        "path": "estate/ingest (raw NC DPS)",
    },
)


def tag_census() -> list[dict[str, str]]:
    """Exact existing-tag census. Empty invented list is the contract."""
    return [dict(row) for row in TAG_CENSUS]


def _split_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[str] = []
        for item in value:
            out.extend(_split_tokens(item))
        return out
    if isinstance(value, dict):
        return []
    text = str(value).strip()
    if not text:
        return []
    if "|" in text:
        return [part.strip() for part in text.split("|") if part.strip()]
    return [text]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def collect_existing_code_vacancy_tags(*rows: Optional[dict]) -> list[str]:
    """Return existing NC CODE/VACANCY tags found on the rows, in first-seen order.

    Unknown tag-like strings are ignored — never invented, never guessed.
    """
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        blobs: list[Any] = [
            row.get("signal_tags"),
            row.get("tags"),
        ]
        raw_meta = _as_mapping(row.get("raw_meta"))
        blobs.append(raw_meta.get("signal_tags"))
        for token in _split_tokens(blobs):
            if token in EXISTING_CODE_VACANCY_TAGS and token not in seen:
                seen.add(token)
                found.append(token)
    return found


def collect_existing_code_vacancy_facts(*rows: Optional[dict]) -> list[str]:
    """Existing source_types / trigger class / T8 signal on the same paths."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        if token in FACT_TO_TOKENS and token not in seen:
            seen.add(token)
            found.append(token)

    for row in rows:
        if not row:
            continue
        for token in _split_tokens(row.get("source_type")):
            _add(token)
        for token in _split_tokens(row.get("signal")):
            _add(token)
        for token in _split_tokens(row.get("trigger_classes")):
            _add(token)
        scores = _as_mapping(row.get("scores"))
        for token in _split_tokens(scores.get("trigger_classes")):
            _add(token)
        emerging = row.get("emerging_signals")
        nc = _as_mapping(_as_mapping(emerging).get("nc_dps")) if isinstance(emerging, dict) else {}
        for token in _split_tokens(nc.get("signal_types")):
            _add(token)
        for token in _split_tokens(nc.get("trigger_classes")):
            _add(token)
        raw_meta = _as_mapping(row.get("raw_meta"))
        nc_meta = _as_mapping(raw_meta.get("nc_dps"))
        for token in _split_tokens(nc_meta.get("signal_types")):
            _add(token)
        for token in _split_tokens(nc_meta.get("trigger_classes")):
            _add(token)
    return found


def tokens_from_existing_tags(tags: Iterable[str]) -> list[str]:
    """Map existing tags to existing recipe tokens. Unknown tags are dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        for token in TAG_TO_TOKENS.get(str(tag), ()):
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def expand_recipe_tokens(tokens: Iterable[str]) -> list[str]:
    """Replace existing NC tags / facts with the recipe tokens they already mean.

    Unknown tokens pass through unchanged. No invented tags.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        token = str(raw).strip()
        if not token:
            continue
        promoted = list(TAG_TO_TOKENS.get(token, ())) + list(FACT_TO_TOKENS.get(token, ()))
        items = promoted or [token]
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


# A disjunctive source proves "vacancy OR code". Promoting it to both facts
# would assert two things custody never said. Recipe tokens (scoring) are a
# separate binding and are not decided here.
DISJUNCTIVE_SOURCES = frozenset({TAG_NC_VACANCY_OR_CODE, TRIGGER_VACANCY_OR_CODE})


def _single_meaning_sources(items: Iterable[str], token: str) -> list[str]:
    return [
        item for item in items
        if item not in DISJUNCTIVE_SOURCES
        and token in TAG_TO_TOKENS.get(item, FACT_TO_TOKENS.get(item, ()))
    ]


def _disjunctive_fact(ingredient_id: str, disjunctive: list[str]) -> dict[str, Any]:
    """UNKNOWN: the only evidence is a vacancy-or-code source."""
    fact = _fact(ingredient_id, known=False, source=disjunctive[0])
    fact["reason"] = "DISJUNCTIVE_TAG_ONLY"
    fact["why"] = (
        f"{disjunctive[0]} means vacancy OR code; it proves neither {ingredient_id} "
        "nor its absence. See code_vacancy.either."
    )
    return fact


def _fact(ingredient_id: str, *, known: bool, source: Optional[str]) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_CODE_VACANCY)
    return {
        "ingredient_id": ingredient_id,
        "family": FAMILY_CODE_VACANCY,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "PROMOTION",
        "state": STATE_KNOWN if known else STATE_UNKNOWN,
        "value": True if known else None,
        "source_field": source,
        "reason": (
            "PROMOTION_OF_EXISTING_TAG"
            if known
            else "EXISTING_TAG_NOT_ON_ROW"
        ),
        "why": (
            f"Promoted existing tagged fact {ingredient_id} from {source}."
            if known
            else (
                "NC CODE/VACANCY tags already exist on the DPS ingest path "
                "but were not on this query row. Bind signal_tags / overlay."
            )
        ),
    }


def project_code_vacancy_facts(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Promote existing NC CODE/VACANCY tags onto a queryable fact block.

    Missing tags stay UNKNOWN (never a fabricated false).
    """
    row = dict(estate_row or {})
    overlay = dict(ingested or {})
    tags = collect_existing_code_vacancy_tags(overlay, row)
    facts = collect_existing_code_vacancy_facts(overlay, row)
    promoted = tokens_from_existing_tags(tags)
    for fact in facts:
        for token in FACT_TO_TOKENS[fact]:
            if token not in promoted:
                promoted.append(token)

    found = [*tags, *facts]
    code_sources = _single_meaning_sources(found, TOKEN_CODE_VIOLATION)
    vacancy_sources = _single_meaning_sources(found, TOKEN_VACANCY)
    disjunctive = [item for item in found if item in DISJUNCTIVE_SOURCES]

    def _specific(ingredient_id: str, sources: list[str]) -> dict[str, Any]:
        if sources:
            return _fact(ingredient_id, known=True, source=sources[0])
        if disjunctive:
            return _disjunctive_fact(ingredient_id, disjunctive)
        return _fact(ingredient_id, known=False, source=None)

    either_sources = [*code_sources, *vacancy_sources, *disjunctive]
    block = {
        INGREDIENT_CODE_VIOLATION: _specific(INGREDIENT_CODE_VIOLATION, code_sources),
        INGREDIENT_VACANCY: _specific(INGREDIENT_VACANCY, vacancy_sources),
        INGREDIENT_CODE_OR_VACANCY: _fact(
            INGREDIENT_CODE_OR_VACANCY,
            known=bool(either_sources),
            source=either_sources[0] if either_sources else None,
        ),
    }
    gap, mode = unlock_classes(FAMILY_CODE_VACANCY)
    return {
        "family": FAMILY_CODE_VACANCY,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "tags_found": tags,
        "facts_found": facts,
        "recipe_tokens": promoted,
        "invented_tags": [],
        "facts": block,
    }
