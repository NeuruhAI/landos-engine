"""Aggregate yield reports. Counts only — no phones, no owner names, no PII."""
from __future__ import annotations

from typing import Dict, Iterable, List

PII_FORBIDDEN_COLUMNS = {
    "owner_name",
    "phone",
    "phone_1",
    "phone_2",
    "phone_3",
    "mailing_address",
    "property_address_raw",
    "matched_person",
    "apn",
    "candidate_id",
}

RECIPE_CONTACT_COLUMNS = (
    "primary_recipe",
    "recipe_version",
    "candidates",
    "qualified",
    "research",
    "identity_resolved",
    "phone_appended_high",
    "medium_review",
    "review_held",
    "unresolved",
    "still_missing",
    "phone_fill_rate",
    "high_confidence_rate",
    "living_person_appended",
    "heir_appended",
    "entity_appended",
    "counties",
)

RECIPE_OWNER_CLASS_COLUMNS = (
    "primary_recipe",
    "owner_class",
    "candidates",
    "qualified",
    "identity_resolved",
    "phone_appended_high",
    "medium_review",
    "review_held",
    "unresolved",
    "still_missing",
    "phone_fill_rate",
)

COUNTY_YIELD_COLUMNS = (
    "county",
    "fips",
    "candidates",
    "qualified",
    "identity_resolved",
    "phone_appended_high",
    "medium_review",
    "review_held",
    "no_identity_match",
    "unresolved",
    "still_missing",
    "phone_fill_rate",
    "living_person",
    "multi_person",
    "deceased_estate_heirs",
    "heirs",
    "trust",
    "entity",
    "entity_trust_gov",
    "unknown",
)


def _is_qualified(row: dict) -> bool:
    return str(row.get("qualification_state") or "").startswith("QUALIFIED")


def _appended(row: dict) -> bool:
    if str(row.get("phone_present") or "").upper() == "YES":
        return True
    return str(row.get("contact_resolution_state") or "") == "PHONE_APPENDED_HIGH"


# A resolution state answers exactly one question: did we reach a living,
# evidenced identity, and if not, why not. Held is not the same as unresolved:
# held means we found something and refused it.
IDENTITY_RESOLVED_STATES = {
    "PHONE_APPENDED_HIGH",
    "IDENTITY_MATCHED_VOTER_PHONE_ABSENT",
}

REVIEW_HELD_STATES = {
    "TIER_C_HELD_NOT_APPENDED",
    "AMBIGUOUS_REJECTED",
    "COMMON_NAME_REJECTED",
}

NO_IDENTITY_MATCH_STATES = {
    "NO_ADDRESS_CORROBORATED_NCSBE_MATCH",
}


def _identity_resolved(row: dict) -> bool:
    if _appended(row):
        return True
    return str(row.get("contact_resolution_state") or "") in IDENTITY_RESOLVED_STATES


def _review_held(row: dict) -> bool:
    return str(row.get("contact_resolution_state") or "") in REVIEW_HELD_STATES


def _no_identity_match(row: dict) -> bool:
    return str(row.get("contact_resolution_state") or "") in NO_IDENTITY_MATCH_STATES


def _medium_review(row: dict) -> bool:
    """Evidence exists but did not clear HIGH — never auto-appended."""
    if _appended(row):
        return False
    return str(row.get("confidence_category") or "").upper() == "MEDIUM"


def _unresolved(row: dict) -> bool:
    """Neither appended, nor identity-resolved, nor explicitly held."""
    return not (
        _identity_resolved(row)
        or _review_held(row)
        or _medium_review(row)
        or _no_identity_match(row)
    )


def _rate(numerator: int, total: int) -> str:
    if total <= 0:
        return "0.000000"
    return f"{numerator / total:.6f}"


def _fill_rate(appended: int, total: int) -> str:
    return _rate(appended, total)


def _tally(bucket: dict, row: dict) -> None:
    """Shared per-row accounting so every report answers the same question."""
    bucket["candidates"] += 1
    if _is_qualified(row):
        bucket["qualified"] += 1
    elif "research" in bucket:
        bucket["research"] += 1
    if _identity_resolved(row):
        bucket["identity_resolved"] += 1
    if _appended(row):
        bucket["phone_appended_high"] += 1
    else:
        bucket["still_missing"] += 1
    if _medium_review(row):
        bucket["medium_review"] += 1
    if _review_held(row):
        bucket["review_held"] += 1
    if "no_identity_match" in bucket and _no_identity_match(row):
        bucket["no_identity_match"] += 1
    if _unresolved(row):
        bucket["unresolved"] += 1


def recipe_contact_yield(ledger: List[dict]) -> List[dict]:
    buckets: Dict[str, dict] = {}
    counties: Dict[str, set] = {}
    versions: Dict[str, str] = {}
    for row in ledger:
        recipe = row.get("primary_recipe") or "(blank)"
        bucket = buckets.setdefault(
            recipe,
            {
                "primary_recipe": recipe,
                "candidates": 0,
                "qualified": 0,
                "research": 0,
                "identity_resolved": 0,
                "phone_appended_high": 0,
                "medium_review": 0,
                "review_held": 0,
                "unresolved": 0,
                "still_missing": 0,
                "living_person_appended": 0,
                "heir_appended": 0,
                "entity_appended": 0,
            },
        )
        _tally(bucket, row)
        counties.setdefault(recipe, set())
        if row.get("county"):
            counties[recipe].add(str(row["county"]))
        if not versions.get(recipe) and row.get("recipe_version"):
            versions[recipe] = str(row["recipe_version"])
        if _appended(row):
            oc = row.get("owner_class") or ""
            if oc in {"LIVING_PERSON", "MULTI_PERSON"}:
                bucket["living_person_appended"] += 1
            elif oc in {"HEIRS", "ESTATE", "DECEASED"}:
                bucket["heir_appended"] += 1
            elif oc in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"}:
                bucket["entity_appended"] += 1
    out = []
    for recipe in sorted(buckets, key=lambda r: (-buckets[r]["candidates"], r)):
        row = dict(buckets[recipe])
        row["recipe_version"] = versions.get(recipe, "")
        row["phone_fill_rate"] = _rate(row["phone_appended_high"], row["candidates"])
        row["high_confidence_rate"] = _rate(row["phone_appended_high"], row["identity_resolved"])
        row["counties"] = ";".join(sorted(counties.get(recipe, ())))
        out.append(row)
    return out


def recipe_owner_class_yield(ledger: List[dict]) -> List[dict]:
    buckets: Dict[tuple, dict] = {}
    for row in ledger:
        recipe = row.get("primary_recipe") or "(blank)"
        oc = row.get("owner_class") or "UNKNOWN"
        key = (recipe, oc)
        bucket = buckets.setdefault(
            key,
            {
                "primary_recipe": recipe,
                "owner_class": oc,
                "candidates": 0,
                "qualified": 0,
                "identity_resolved": 0,
                "phone_appended_high": 0,
                "medium_review": 0,
                "review_held": 0,
                "unresolved": 0,
                "still_missing": 0,
            },
        )
        _tally(bucket, row)
    out = []
    for key in sorted(buckets, key=lambda k: (-buckets[k]["candidates"], k[0], k[1])):
        row = dict(buckets[key])
        row["phone_fill_rate"] = _rate(row["phone_appended_high"], row["candidates"])
        out.append(row)
    return out


def county_free_contact_yield(ledger: List[dict]) -> List[dict]:
    buckets: Dict[str, dict] = {}
    fips_for: Dict[str, str] = {}
    for row in ledger:
        county = row.get("county") or "(blank)"
        fips_for.setdefault(county, row.get("fips") or "")
        bucket = buckets.setdefault(
            county,
            {
                "county": county,
                "candidates": 0,
                "qualified": 0,
                "identity_resolved": 0,
                "phone_appended_high": 0,
                "medium_review": 0,
                "review_held": 0,
                "no_identity_match": 0,
                "unresolved": 0,
                "still_missing": 0,
                "living_person": 0,
                "multi_person": 0,
                "deceased_estate_heirs": 0,
                "heirs": 0,
                "trust": 0,
                "entity": 0,
                "entity_trust_gov": 0,
                "unknown": 0,
            },
        )
        _tally(bucket, row)
        oc = row.get("owner_class") or "UNKNOWN"
        if oc == "LIVING_PERSON":
            bucket["living_person"] += 1
        elif oc == "MULTI_PERSON":
            bucket["multi_person"] += 1
        elif oc in {"DECEASED", "ESTATE", "HEIRS"}:
            bucket["deceased_estate_heirs"] += 1
        elif oc in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT", "TRUST", "GOVERNMENT"}:
            bucket["entity_trust_gov"] += 1
        else:
            bucket["unknown"] += 1
        # Narrower owner cuts the founder asked for by name.
        if oc == "HEIRS":
            bucket["heirs"] += 1
        elif oc == "TRUST":
            bucket["trust"] += 1
        elif oc in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"}:
            bucket["entity"] += 1
    out = []
    for county in sorted(buckets, key=lambda c: (-buckets[c]["candidates"], c)):
        row = dict(buckets[county])
        row["fips"] = fips_for.get(county, "")
        row["phone_fill_rate"] = _rate(row["phone_appended_high"], row["candidates"])
        out.append(row)
    return out


def recipe_intelligence(recipe_rows: List[dict], class_rows: List[dict], *, min_candidates: int = 25) -> dict:
    """Analytical read-out only. Never feeds back into an opportunity score."""
    ranked = [r for r in recipe_rows if r["candidates"] >= min_candidates] or list(recipe_rows)

    def _f(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    if not ranked:
        return {
            "TOP_RECIPE_BY_FREE_PHONE_YIELD": "",
            "TOP_RECIPE_BY_HIGH_CONFIDENCE_RATE": "",
            "LOWEST_RECIPE_BY_FREE_PHONE_YIELD": "",
            "HIGHEST_HEIRS_RECIPE": "",
            "HIGHEST_ENTITY_RECIPE": "",
            "MIN_CANDIDATES_FOR_RANKING": min_candidates,
            "RECIPE_COUNT": len(recipe_rows),
        }

    top_fill = max(ranked, key=lambda r: (_f(r, "phone_fill_rate"), r["candidates"]))
    top_high = max(ranked, key=lambda r: (_f(r, "high_confidence_rate"), r["candidates"]))
    low_fill = min(ranked, key=lambda r: (_f(r, "phone_fill_rate"), -r["candidates"]))

    def _class_leader(classes: set) -> str:
        best, best_n = "", 0
        totals: Dict[str, int] = {}
        for row in class_rows:
            if row.get("owner_class") in classes:
                key = row.get("primary_recipe") or "(blank)"
                totals[key] = totals.get(key, 0) + int(row.get("candidates") or 0)
        for key in sorted(totals):
            if totals[key] > best_n:
                best, best_n = key, totals[key]
        return f"{best} (n={best_n})" if best else ""

    def _label(row: dict, key: str) -> str:
        return f"{row['primary_recipe']} ({row[key]} on n={row['candidates']})"

    return {
        "TOP_RECIPE_BY_FREE_PHONE_YIELD": _label(top_fill, "phone_fill_rate"),
        "TOP_RECIPE_BY_HIGH_CONFIDENCE_RATE": _label(top_high, "high_confidence_rate"),
        "LOWEST_RECIPE_BY_FREE_PHONE_YIELD": _label(low_fill, "phone_fill_rate"),
        "HIGHEST_HEIRS_RECIPE": _class_leader({"HEIRS", "ESTATE", "DECEASED"}),
        "HIGHEST_ENTITY_RECIPE": _class_leader({"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"}),
        "MIN_CANDIDATES_FOR_RANKING": min_candidates,
        "RECIPE_COUNT": len(recipe_rows),
    }


def yield_columns_are_aggregate(columns: Iterable[str]) -> bool:
    return not any(col in PII_FORBIDDEN_COLUMNS for col in columns)
