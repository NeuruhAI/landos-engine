"""export_opportunities.py — T4E Opportunity File Factory (deedsonar_opportunity_csv.v1).

Turns the canonical ranked opportunity universe (recipe-engine
rank_nc_curative_raw_v3 output — NC_CURATIVE_ALL.csv) into portable, ranked,
CRM-native data products:

    per-recipe FULL files  ->  Top-N convenience slices  ->  stack matrix
    ->  ALL_OPPORTUNITIES_MASTER.csv  ->  manifests/receipts

    python -m scripts.export_opportunities \
        --source NC_CURATIVE_ALL.csv --output <dir> \
        [--state NC] [--county Mecklenburg] [--recipe <canonical-id>] \
        [--scope full|top100|top500|top1000] \
        [--pending-signals normalized_signals.jsonl --pending-audit audit.json]

PRIMARY OBJECT: the PROPERTY OPPORTUNITY. Contact fields are OPTIONAL columns
and never determine inclusion; callability/DNC are NOT export concerns.

TRUTH INVARIANTS (enforced, tested):
  UNKNOWN != ZERO / FALSE / NO — an unknown value exports as an EMPTY cell,
  never a fabricated 0, "false", or guessed state. mortgage_balance and
  estimated_equity are empty until a real debt-instrument search lands.
  Recipe assignment, scores, versions, and the global rank are read from the
  canonical source verbatim — never recomputed, never invented here.

GLOBAL RANKING: the source universe carries a canonical cross-recipe ranking
(rank 1..N under scoring_version nc_curative_rank.v3, deterministic tie-break
defined in recipe-engine rank_rows()). global_rank/global_score are therefore
DEFINED and copied from source. If a future source lacks them, they export as
NOT_DEFINED — not as a locally fabricated ordering.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "deedsonar_opportunity_csv.v1"

# The versioned portable schema. Column ORDER is part of the contract.
SCHEMA_COLUMNS = [
    "external_lead_id", "property_key", "parcel_apn",
    "county", "county_fips", "property_state",
    "property_address", "property_city", "property_zip",
    "owner_name", "owner_mailing_address", "owner_mailing_city",
    "owner_mailing_state", "owner_mailing_zip",
    "recipe_id", "recipe_name", "recipe_version", "scoring_version",
    "recipe_rank", "recipe_score", "priority_tier",
    "global_rank", "global_score",
    "signal_count", "signal_codes", "signal_summary",
    "stack_count", "stacked_recipes",
    "primary_opportunity_reason", "secondary_opportunity_reasons",
    "property_value", "assessed_value", "estimated_equity",
    "estimated_equity_pct", "mortgage_balance",
    "tax_delinquent_amount", "tax_years_due",
    "foreclosure_flag", "probate_heir_flag", "absentee_owner_flag",
    "out_of_state_owner_flag", "vacancy_flag", "lien_flag",
    "code_violation_flag",
    "evidence_summary", "evidence_count", "source_names", "source_refs",
    "confidence", "contradictions",
    "phone_1", "phone_2", "email_1", "contact_status",
    "source_run_id", "scoring_run_id", "export_run_id",
    "schema_version", "created_at", "exported_at",
    # FACTS DATE (appended; no existing column moves). facts_as_of is the newest
    # date a county payload carried about the property -- observed, never the
    # adapter's declared constant, never a build/export time. Empty = UNKNOWN.
    # created_at/exported_at are PROVENANCE times, not facts times.
    "facts_as_of", "facts_as_of_basis",
]

FACTS_BASIS_OBSERVED = "observed_event_date"
FACTS_BASIS_UNKNOWN = "unknown"
_FACTS_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- canonical recipe registry -------------------------------------------------
# Discovered (NOT invented) from source: the seven NC curative recipe ids are the
# complete router universe in recipe-engine scripts/rank_nc_curative_raw.py:126-145
# (first-match-wins order preserved below); the set is versioned as a WHOLE by
# nc_curative_recipes.v1 and scored by the nc_curative_rank.v3 pipeline. No
# canonical display names / descriptions / per-recipe versions exist upstream —
# recipe_name therefore mirrors recipe_id (never a fabricated marketing name)
# and the qualification text quotes the router predicate verbatim.
RECIPE_REGISTRY: dict[str, dict[str, Any]] = {
    "stalled_tax_foreclosure_heirship": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "heir AND foreclosure (router order 1)",
        "research_lane": "TITLE_CURATIVE_HEIR",
    },
    "heir_property_acquisition_research": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "heir, not foreclosure (router order 2)",
        "research_lane": "TITLE_CURATIVE_HEIR",
    },
    "tax_foreclosure_fractional_title": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "foreclosure AND fragmented, not heir (router order 3)",
        "research_lane": "TITLE_CURATIVE",
    },
    "tax_foreclosure_curative": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "foreclosure only (router order 4)",
        "research_lane": "TITLE_CURATIVE",
    },
    "fractional_family_ownership": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "fragmented only (router order 5)",
        "research_lane": "TITLE_CURATIVE",
    },
    "long_term_absentee_potential_equity": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "out_of_state AND years_owned >= 20 (router order 6)",
        "research_lane": "ACQUISITION_RESEARCH",
    },
    "published_tax_distress": {
        "status": "ACTIVE", "family": "nc_curative",
        "recipe_version": "nc_curative_recipes.v1", "scoring_version": "nc_curative_rank.v3",
        "qualification_rule": "fallthrough default (router order 7)",
        "research_lane": "ENRICHMENT",
    },
    # Engine-resident sourcing recipe (MI/IN) — ACTIVE in its own lane, carries its
    # version inside the id, no data in the NC curative universe -> no export family
    # in an NC run. Berrien jurisdiction is staged-not-delivered (config markers).
    "senior_lakefront_assemblage_v1": {
        "status": "ACTIVE", "family": "engine_sourcing",
        "recipe_version": "senior_lakefront_assemblage_v1", "scoring_version": "senior_score(D1)",
        "qualification_rule": "senior_score >= 55 AND lake_adjacent",
        "research_lane": "SOURCING",
    },
    # Engine wave-a matcher recommendations (app/nc_dps_wave_a_pipeline.py:177-183).
    # NOTE the namespace collision: published_tax_distress is ALSO defined there with
    # a different rule; this registry keeps the nc_curative definition (the ranked
    # universe's authority) and records the matcher trio as a separate family with
    # no ranked product of their own.
    "tax_distress_absentee_owner": {
        "status": "EXPERIMENTAL", "family": "engine_wave_a_matcher",
        "recipe_version": "NOT_DEFINED", "scoring_version": "NOT_DEFINED",
        "qualification_rule": "tax_distress trigger AND out_of_state (matcher)",
        "research_lane": "ENRICHMENT",
    },
    "long_term_absentee_enrichment": {
        "status": "EXPERIMENTAL", "family": "engine_wave_a_matcher",
        "recipe_version": "NOT_DEFINED", "scoring_version": "NOT_DEFINED",
        "qualification_rule": "out_of_state (matcher)",
        "research_lane": "ENRICHMENT",
    },
}

TOP_SLICES = (100, 500, 1000)
STATE_BY_FIPS_PREFIX = {"37": "NC", "26": "MI", "18": "IN", "45": "SC"}
NOT_DEFINED = "NOT_DEFINED"


# --- helpers -------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_bool(raw: str) -> str:
    """Source booleans ('True'/'False') -> lowercase; UNKNOWN (blank) stays blank —
    an absent fact is never exported as false."""
    v = (raw or "").strip().lower()
    if v in ("true", "1", "yes", "y"):
        return "true"
    if v in ("false", "0", "no", "n"):
        return "false"
    return ""


def norm_num(raw: str) -> str:
    """Plain decimal or EMPTY. Never coerces blank/garbage to 0."""
    v = (raw or "").strip().replace("$", "").replace(",", "")
    if not v:
        return ""
    try:
        f = float(v)
    except ValueError:
        return ""
    return str(int(f)) if f == int(f) else repr(f)


def property_key(row: dict[str, str]) -> str:
    """Stable portable identity: <STATE>:<county_fips>:<parcel_apn> — the same
    convention the engine mint uses for property_id (nc_dps_raw_writer)."""
    fips = (row.get("county_fips") or "").strip()
    state = STATE_BY_FIPS_PREFIX.get(fips[:2], "XX")
    return f"{state}:{fips}:{(row.get('parcel_apn') or '').strip()}"


def parse_score_reasons(raw: str) -> tuple[str, str]:
    """Canonical score_reasons ('name:+pts|name:+pts') -> (primary, secondary)
    ordered by points desc then source order. Derived, never invented."""
    items = []
    for i, part in enumerate((raw or "").split("|")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(.+?):\+?(-?\d+)$", part)
        if m:
            items.append((-int(m.group(2)), i, m.group(1)))
        else:
            items.append((0, i, part))
    if not items:
        return "", ""
    items.sort()
    names = [name for _, _, name in items]
    return names[0], "|".join(names[1:])


def map_row(src: dict[str, str], *, provenance: dict[str, str]) -> dict[str, str]:
    """One canonical ranked-universe row -> one deedsonar_opportunity_csv.v1 row.
    Every UNKNOWN exports as an empty cell (see module invariants)."""
    key = property_key(src)
    recipe_id = (src.get("recipe_id") or "").strip()
    primary, secondary = parse_score_reasons(src.get("score_reasons", ""))
    court = (src.get("court_case_numbers") or "").strip()
    evidence_bits = [b for b in (
        (src.get("source_types") or "").strip(),
        f"court={court}" if court else "",
        f"parcel_match={(src.get('parcel_match_status') or '').strip()}"
        if (src.get("parcel_match_status") or "").strip() else "",
    ) if b]
    score = norm_num(src.get("curative_priority_score", ""))
    rank = (src.get("rank") or "").strip()
    return {
        "external_lead_id": key,
        "property_key": key,
        "parcel_apn": (src.get("parcel_apn") or "").strip(),
        "county": (src.get("county") or "").strip(),
        "county_fips": (src.get("county_fips") or "").strip(),
        "property_state": STATE_BY_FIPS_PREFIX.get((src.get("county_fips") or "")[:2], ""),
        "property_address": (src.get("property_address") or "").strip(),
        "property_city": (src.get("property_city") or "").strip(),
        "property_zip": (src.get("property_zip") or "").strip(),
        "owner_name": (src.get("owner_name") or "").strip(),
        "owner_mailing_address": (src.get("owner_mailing_address") or "").strip(),
        "owner_mailing_city": (src.get("owner_mailing_city") or "").strip(),
        "owner_mailing_state": (src.get("owner_mailing_state") or "").strip(),
        "owner_mailing_zip": (src.get("owner_mailing_zip") or "").strip(),
        "recipe_id": recipe_id,
        # no canonical display name exists upstream — mirror the id, never invent
        "recipe_name": recipe_id,
        "recipe_version": (src.get("recipe_version") or "").strip(),
        "scoring_version": (src.get("scoring_version") or "").strip(),
        "recipe_rank": "",            # assigned after per-recipe sort
        "recipe_score": score,
        "priority_tier": (src.get("priority_tier") or "").strip(),
        "global_rank": rank if rank else NOT_DEFINED,
        "global_score": score if rank else NOT_DEFINED,
        "signal_count": norm_num(src.get("signal_count", "")),
        "signal_codes": (src.get("source_types") or "").strip(),
        "signal_summary": (src.get("trigger_classes") or "").strip(),
        "stack_count": "",            # assigned from the descriptive stack matrix
        "stacked_recipes": "",        # (canonical data assigns ONE recipe per property)
        "primary_opportunity_reason": primary,
        "secondary_opportunity_reasons": secondary,
        "property_value": norm_num(src.get("market_value", "")),
        "assessed_value": norm_num(src.get("assessed_value", "")),
        "estimated_equity": norm_num(src.get("estimated_equity", "")),
        "estimated_equity_pct": "",
        "mortgage_balance": norm_num(src.get("mortgage_balance", "")),
        "tax_delinquent_amount": norm_num(src.get("tax_balance", "")),
        "tax_years_due": (src.get("tax_years") or "").strip(),
        "foreclosure_flag": norm_bool(src.get("tax_foreclosure_indicator", "")),
        "probate_heir_flag": norm_bool(src.get("heir_indicator", "")),
        "absentee_owner_flag": "",    # not canonically computed upstream -> UNKNOWN
        "out_of_state_owner_flag": norm_bool(src.get("out_of_state", "")),
        "vacancy_flag": "",           # UNKNOWN
        "lien_flag": "",              # judgment_lien_balance never populated -> UNKNOWN
        "code_violation_flag": "",    # UNKNOWN
        "evidence_summary": "; ".join(evidence_bits),
        "evidence_count": norm_num(src.get("signal_count", "")),
        "source_names": (src.get("source_families") or "").strip(),
        "source_refs": (src.get("source_urls") or src.get("source_url") or "").strip(),
        "confidence": "",             # no canonical per-row numeric confidence exists
        "contradictions": "",
        "phone_1": (src.get("phone") or "").strip(),
        "phone_2": "",
        "email_1": "",
        "contact_status": (src.get("phone_status") or "").strip(),
        **provenance,
        **facts_date(src),
    }


def facts_date(src: dict[str, str]) -> dict[str, str]:
    """Carry the upstream facts date verbatim, or say UNKNOWN.

    Only a value the enrichment stage labelled `observed_event_date` passes; any
    other basis (or a malformed date) exports as empty/unknown. Nothing here
    derives a date -- in particular not from created_at or exported_at."""
    basis = (src.get("facts_as_of_basis") or "").strip()
    value = (src.get("facts_as_of") or "").strip()
    if basis == FACTS_BASIS_OBSERVED and _FACTS_DATE.match(value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            pass
        else:
            return {"facts_as_of": value, "facts_as_of_basis": FACTS_BASIS_OBSERVED}
    return {"facts_as_of": "", "facts_as_of_basis": FACTS_BASIS_UNKNOWN}


def _count_basis(rows: list[dict[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["facts_as_of_basis"]] = out.get(r["facts_as_of_basis"], 0) + 1
    return out


def sort_key(row: dict[str, str]):
    """Deterministic order: score desc -> canonical global rank asc -> key asc."""
    score = float(row["recipe_score"]) if row["recipe_score"] else -1.0
    grank = (int(row["global_rank"])
             if row["global_rank"] and row["global_rank"] != NOT_DEFINED else 1 << 30)
    return (-score, grank, row["property_key"])


# --- factory -------------------------------------------------------------------

def load_source(path: Path) -> list[dict[str, str]]:
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    if not rows:
        raise SystemExit(f"source has no rows: {path}")
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SCHEMA_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def file_entry(path: Path, rows: list[dict[str, str]], *, recipe_id: str,
               sort_field: str, source_runs: list[str]) -> dict[str, Any]:
    scores = [float(r["recipe_score"]) for r in rows if r["recipe_score"]]
    reg = RECIPE_REGISTRY.get(recipe_id, {})
    return {
        "schema_version": SCHEMA_VERSION,
        "filename": path.name,
        "recipe_id": recipe_id,
        "recipe_version": reg.get("recipe_version", ""),
        "scoring_version": reg.get("scoring_version", ""),
        "generated_at": None,          # stamped by build() with export ts
        "row_count": len(rows),
        "unique_property_count": len({r["property_key"] for r in rows}),
        "sort_field": sort_field,
        "sort_direction": "desc",
        "highest_score": max(scores) if scores else None,
        "lowest_score": min(scores) if scores else None,
        "source_runs": source_runs,
        "sha256": sha256_file(path),
    }


def build(source: Path, output: Path, *, state: str | None, county: str | None,
          recipe: str | None, scope: str, pending_signals: Path | None,
          pending_audit: Path | None, meck_staging: Path | None,
          exported_at: str | None, export_run_id: str | None,
          ranked_universe_date: str | None = None) -> dict[str, Any]:
    src_rows = load_source(source)
    source_sha = sha256_file(source)
    ts = exported_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    run_id = export_run_id or "exprun_" + hashlib.sha256(
        f"{source_sha}|{SCHEMA_VERSION}|{state}|{county}|{recipe}|{scope}".encode()
    ).hexdigest()[:16]
    provenance = {
        "source_run_id": f"sha256:{source_sha[:16]}",
        "scoring_run_id": NOT_DEFINED,   # no canonical scoring run-id artifact exists
        "export_run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        # created_at = the ranked-universe run date, when the caller states it
        # (--ranked-universe-date); exported_at = this export. It used to be the
        # literal "2026-08-12" on every export, and DeedSonar read it as the
        # facts date. Unstated is empty -- UNKNOWN, never a remembered day.
        # Neither column is a facts date: that is facts_as_of.
        "created_at": (ranked_universe_date or "").strip(),
        "exported_at": ts,
    }

    mapped = [map_row(r, provenance=provenance) for r in src_rows]

    # filters (FULL = canonical universe; filters narrow the product deliberately)
    if state:
        mapped = [r for r in mapped if r["property_state"] == state.upper()]
    if county:
        mapped = [r for r in mapped if r["county"].lower() == county.lower()]
    if recipe:
        mapped = [r for r in mapped if r["recipe_id"] == recipe]

    dupes = [k for k, n in _count(mapped).items() if n > 1]
    if dupes:
        raise SystemExit(f"canonical source repeats property keys: {dupes[:5]}")

    # descriptive stack facts (canonical data assigns exactly ONE recipe/property;
    # matched_recipe_count is reported as-is, no cross-recipe strategy is invented)
    for r in mapped:
        r["stack_count"] = "1"
        r["stacked_recipes"] = r["recipe_id"]

    out: dict[str, list[Path]] = {"full": [], "top100": [], "top500": [], "top1000": []}
    index_rows: list[dict[str, Any]] = []
    manifest_files: list[dict[str, Any]] = []
    source_runs = [f"sha256:{source_sha[:16]}:{source.name}"]

    by_recipe: dict[str, list[dict[str, str]]] = {}
    for r in mapped:
        by_recipe.setdefault(r["recipe_id"], []).append(r)

    d_full = output / "03_FULL_RECIPE_EXPORTS"
    d_top = {100: output / "04_TOP100", 500: output / "05_TOP500", 1000: output / "06_TOP1000"}

    for rid in sorted(by_recipe):
        rows = sorted(by_recipe[rid], key=sort_key)
        for i, r in enumerate(rows, start=1):
            r["recipe_rank"] = str(i)
        if scope in ("full", "all"):
            p = d_full / f"{rid}_FULL.csv"
            write_csv(p, rows)
            manifest_files.append(file_entry(p, rows, recipe_id=rid,
                                             sort_field="recipe_score", source_runs=source_runs))
            out["full"].append(p)
        # Top-N slices are conveniences over FULL: emitted only when the recipe's
        # universe actually exceeds N (a slice equal to FULL would be a duplicate,
        # and a shorter file would be arbitrary truncation).
        for n in TOP_SLICES:
            if scope not in ("all", f"top{n}"):
                continue
            if len(rows) > n:
                p = d_top[n] / f"{rid}_TOP_{n}.csv"
                write_csv(p, rows[:n])
                manifest_files.append(file_entry(p, rows[:n], recipe_id=rid,
                                                 sort_field="recipe_score", source_runs=source_runs))
                out[f"top{n}"].append(p)

    # --- stack matrix (property x recipe membership; descriptive only) ----------
    stack_rows = []
    for r in sorted(mapped, key=sort_key):
        stack_rows.append({
            "property_key": r["property_key"],
            "matched_recipe_count": r["stack_count"],
            "matched_recipe_ids": r["stacked_recipes"],
            "matched_recipe_versions": r["recipe_version"],
            "signal_count": r["signal_count"],
            "highest_recipe_score": r["recipe_score"],
            "primary_recipe": r["recipe_id"],
        })
    p_stack = output / "07_STACKED" / "PROPERTY_RECIPE_STACK_MATRIX.csv"
    p_stack.parent.mkdir(parents=True, exist_ok=True)
    with open(p_stack, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(stack_rows[0].keys()))
        w.writeheader()
        w.writerows(stack_rows)

    # --- master (one canonical property per row, canonical global order) --------
    master = sorted(mapped, key=lambda r: (int(r["global_rank"])
                    if r["global_rank"] != NOT_DEFINED else 1 << 30, r["property_key"]))
    p_master = output / "08_MASTER" / "ALL_OPPORTUNITIES_MASTER.csv"
    write_csv(p_master, master)
    master_entry = file_entry(p_master, master, recipe_id="ALL",
                              sort_field="global_rank", source_runs=source_runs)
    master_entry["sort_direction"] = "asc"
    manifest_files.append(master_entry)

    # DeedSonar county-import-ready master (canonical headers, stable source_row_id)
    p_ds = output / "08_MASTER" / "DEEDSONAR_IMPORT_READY_MASTER.csv"
    write_deedsonar_import(p_ds, master)
    ds_entry = file_entry(p_ds, master, recipe_id="ALL",
                          sort_field="global_rank", source_runs=source_runs)
    ds_entry["sort_direction"] = "asc"
    ds_entry["schema_version"] = f"{SCHEMA_VERSION}+deedsonar_county_import"
    manifest_files.append(ds_entry)

    # --- pending, never-fabricated signals --------------------------------------
    pending_summary = None
    if pending_signals and pending_signals.exists():
        pending_summary = _write_pending(output / "12_PENDING_UNRANKED",
                                         pending_signals, pending_audit, meck_staging)

    # --- manifests ---------------------------------------------------------------
    for e in manifest_files:
        e["generated_at"] = ts
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "export_run_id": run_id,
        "generated_at": ts,
        "source": {"path": str(source), "sha256": source_sha, "rows": len(src_rows)},
        "filters": {"state": state, "county": county, "recipe": recipe, "scope": scope},
        "global_ranking": {"defined": True, "scoring_version": "nc_curative_rank.v3",
                           "source_field": "rank"},
        "files": manifest_files,
        "stack_matrix": {"filename": p_stack.name, "rows": len(stack_rows),
                         "sha256": sha256_file(p_stack),
                         "semantics": "descriptive matched_recipe_count only; canonical "
                                      "source assigns one recipe per property; no cross-"
                                      "recipe strategy is defined upstream"},
        "pending_unranked": pending_summary,
        "facts_as_of": {
            "basis_counts": dict(sorted(_count_basis(mapped).items())),
            "min": min((r["facts_as_of"] for r in mapped if r["facts_as_of"]), default=None),
            "max": max((r["facts_as_of"] for r in mapped if r["facts_as_of"]), default=None),
            "semantics": "newest county-payload date per property (observed_event_date); "
                         "empty = UNKNOWN; never created_at/exported_at",
        },
        "contact_gate": {"phone_export_gate": False, "dnc_export_gate": False,
                         "callability_export_gate": False,
                         "note": "contact fields optional; inclusion never depends on them"},
    }
    d_man = output / "09_MANIFESTS"
    d_man.mkdir(parents=True, exist_ok=True)
    (d_man / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    with open(d_man / "CSV_FILE_INDEX.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["filename", "recipe_id", "row_count",
                                           "unique_property_count", "sha256"])
        w.writeheader()
        for e in manifest_files:
            w.writerow({k: e[k] for k in w.fieldnames})
    receipt = {
        "export_run_id": run_id, "generated_at": ts,
        "file_count": len(manifest_files),
        "total_rows": sum(e["row_count"] for e in manifest_files),
        "unique_properties": len({r["property_key"] for r in mapped}),
        "manifest_sha256": sha256_file(d_man / "MANIFEST.json"),
    }
    (d_man / "EXPORT_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    return manifest


def _count(rows: list[dict[str, str]]) -> dict[str, int]:
    c: dict[str, int] = {}
    for r in rows:
        c[r["property_key"]] = c.get(r["property_key"], 0) + 1
    return c


def _write_pending(outdir: Path, signals: Path, audit: Path | None,
                   meck_staging: Path | None) -> dict[str, Any]:
    """Signals that have NOT passed canonical recipe/ranking are inventoried —
    counts + source references only. They are NEVER fabricated into ranked rows."""
    outdir.mkdir(parents=True, exist_ok=True)
    by_county: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_cell: dict[tuple[str, str], int] = {}
    keys = set()
    n = 0
    with open(signals, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            s = json.loads(line)
            n += 1
            by_county[s["geography"]] = by_county.get(s["geography"], 0) + 1
            by_type[s["source_type"]] = by_type.get(s["source_type"], 0) + 1
            cell = (s["geography"], s["source_type"])
            by_cell[cell] = by_cell.get(cell, 0) + 1
            keys.add((s["geography"], s["parcel_apn"]))
    summary = {
        "status": "SIGNALS_PENDING_RANKING",
        "reason": "collected by landos-ingest current_official_distress_v3 (live 2026-08-12) "
                  "but not yet passed through canonical recipe routing / nc_curative_rank "
                  "scoring — exporting them as ranked opportunities would fabricate rank",
        "signal_count": n,
        "distinct_property_keys": len(keys),
        "by_county_fips": by_county,
        "by_source_type": by_type,
        "source_jsonl": {"path": str(signals), "sha256": sha256_file(signals)},
        "source_audit": {"path": str(audit), "sha256": sha256_file(audit)} if audit and audit.exists() else None,
        "mecklenburg_unresolved_staging": None,
    }
    if meck_staging and meck_staging.exists():
        m = sum(1 for line in open(meck_staging, encoding="utf-8") if line.strip())
        summary["mecklenburg_unresolved_staging"] = {
            "rows": m, "path": str(meck_staging), "sha256": sha256_file(meck_staging),
            "note": "taxpayer-level distress with NO parcel identity; needs owner->parcel "
                    "resolution (T5 lane) before it can even enter recipe routing",
        }
    (outdir / "SIGNALS_PENDING_RANKING.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    with open(outdir / "SIGNALS_PENDING_RANKING.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["county_fips", "source_type", "signal_count"])
        for (fips, st) in sorted(by_cell):
            w.writerow([fips, st, by_cell[(fips, st)]])
    return summary


# --- DeedSonar county-import-ready file ----------------------------------------
# DeedSonar's governed importer (county-import Path A) auto-maps ONLY its own
# canonical headers/aliases (deedsonar src/lib/imports/county-normalize.ts:12-42);
# `parcel_apn` is NOT an alias, `state` is REQUIRED per row, and a derived
# source_row_id is file-sha-scoped. This emitter therefore writes DeedSonar's
# canonical headers verbatim (perfect auto-map, no wizard hand-mapping) with an
# EXPLICIT source_row_id = property_key (identity stable across re-generated
# files). Every deedsonar_opportunity_csv.v1 column additionally rides along —
# unmapped headers land verbatim in acquisition_leads.source_raw_payload jsonb
# (deedsonar migration 107:632), the only durable home DeedSonar offers for
# recipe/score/version metadata today (see DEEDSONAR_EXPORT_GAPS.md).

DEEDSONAR_CANONICAL_HEADERS = [
    "state", "county", "source_system", "source_url", "source_row_id",
    "apn", "property_address", "owner_name",
    "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "assessed_value", "market_value", "tax_amount_owed", "acreage",
    "signal_tags", "notes",
]


def deedsonar_import_row(r: dict[str, str]) -> dict[str, str]:
    """v1 row -> DeedSonar canonical columns. UNKNOWN stays EMPTY (their importer
    reads blank as NULL, never 0). dnc/phone/email are deliberately OMITTED:
    no phones exist and an absent DNC fact must not be exported at all."""
    return {
        "state": r["property_state"],
        "county": r["county"],
        "source_system": "landos_engine",
        "source_url": (r["source_refs"].split("|", 1)[0] if r["source_refs"] else ""),
        "source_row_id": r["property_key"],
        "apn": r["parcel_apn"],
        "property_address": r["property_address"],
        "owner_name": r["owner_name"],
        "mailing_address": r["owner_mailing_address"],
        "mailing_city": r["owner_mailing_city"],
        "mailing_state": r["owner_mailing_state"],
        "mailing_zip": r["owner_mailing_zip"],
        "assessed_value": r["assessed_value"],
        "market_value": r["property_value"],
        "tax_amount_owed": r["tax_delinquent_amount"],
        "acreage": "",
        "signal_tags": r["signal_summary"],
        "notes": (f"recipe={r['recipe_id']} tier={r['priority_tier']} "
                  f"score={r['recipe_score']} global_rank={r['global_rank']} "
                  f"{r['scoring_version']} {r['recipe_version']}"),
    }


def write_deedsonar_import(path: Path, rows: list[dict[str, str]]) -> None:
    """Canonical headers first (auto-mapped), full v1 columns appended (ride into
    source_raw_payload verbatim — the recipe-metadata custody channel)."""
    extras = [c for c in SCHEMA_COLUMNS if c not in DEEDSONAR_CANONICAL_HEADERS]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DEEDSONAR_CANONICAL_HEADERS + extras)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: r.get(c, "") for c in extras}, **deedsonar_import_row(r)})


# --- registry emit -------------------------------------------------------------

def write_registry(outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    fields = ["recipe_id", "recipe_name", "recipe_version", "scoring_version", "status",
              "family", "qualification_rule", "research_lane"]
    with open(outdir / "OPPORTUNITY_RECIPE_REGISTRY.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for rid, meta in RECIPE_REGISTRY.items():
            w.writerow({"recipe_id": rid, "recipe_name": rid, **{k: meta.get(k, "") for k in fields[2:]}})


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="T4E Opportunity File Factory (deedsonar_opportunity_csv.v1)")
    ap.add_argument("--source", required=True, type=Path, help="canonical ranked universe CSV (NC_CURATIVE_ALL.csv)")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--state")
    ap.add_argument("--county")
    ap.add_argument("--recipe", choices=sorted(RECIPE_REGISTRY))
    ap.add_argument("--scope", default="all", choices=["all", "full", "top100", "top500", "top1000"])
    ap.add_argument("--pending-signals", type=Path)
    ap.add_argument("--pending-audit", type=Path)
    ap.add_argument("--meck-staging", type=Path)
    ap.add_argument("--exported-at", help="ISO ts override (deterministic re-runs)")
    ap.add_argument("--export-run-id")
    ap.add_argument("--ranked-universe-date",
                    help="date the ranked universe was produced (provenance -> created_at); "
                         "omitted = empty. NOT a facts date")
    args = ap.parse_args()
    manifest = build(args.source, args.output, state=args.state, county=args.county,
                     recipe=args.recipe, scope=args.scope,
                     pending_signals=args.pending_signals, pending_audit=args.pending_audit,
                     meck_staging=args.meck_staging, exported_at=args.exported_at,
                     export_run_id=args.export_run_id,
                     ranked_universe_date=args.ranked_universe_date)
    write_registry(args.output / "02_RECIPE_REGISTRY")
    master = next(e for e in manifest["files"] if e["recipe_id"] == "ALL")
    print(json.dumps({
        "export_run_id": manifest["export_run_id"],
        "files": len(manifest["files"]),
        "unique_properties": master["unique_property_count"],
        "pending_unranked": (manifest["pending_unranked"] or {}).get("signal_count"),
    }, indent=2))


if __name__ == "__main__":
    main()
