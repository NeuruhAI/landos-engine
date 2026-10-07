"""T20C canonical free-contact pipeline: delta refresh, resolution, yield, $0."""
from __future__ import annotations

import json
from pathlib import Path

from app.contact.delta_refresh import (
    infer_prior_estate_ids,
    partition_delta,
    refresh_receipt,
)
from app.contact.identity_tuple import identity_tuple_hash
from app.contact.official_fetch import fetch_official_bytes, official_url_allowed
from app.contact.product_export import overlay_product_rows
from app.contact.resolution import (
    CALLABILITY_NOT_AUTHORIZED,
    PHONE_APPENDED_HIGH,
    build_resolution_ledger,
    classify_resolution_state,
    prioritize_estate,
)
from app.contact.t12_append import CONTACT_PROVENANCE_FIELDS
from app.contact.yield_reports import (
    COUNTY_YIELD_COLUMNS,
    PII_FORBIDDEN_COLUMNS,
    RECIPE_CONTACT_COLUMNS,
    RECIPE_OWNER_CLASS_COLUMNS,
    county_free_contact_yield,
    recipe_contact_yield,
    recipe_owner_class_yield,
    yield_columns_are_aggregate,
)


def _estate_row(**kwargs) -> dict:
    base = {
        "candidate_id": "c1",
        "apn": "100",
        "county": "Wake",
        "fips": "37183",
        "owner_name": "RANDALL A WOLF",
        "mailing_address": "1 MAIN ST",
        "mailing_city": "RALEIGH",
        "mailing_state": "NC",
        "mailing_zip": "27601",
        "property_address_raw": "1 MAIN ST",
        "property_address_normalized": "1 MAIN ST",
        "property_zip": "27601",
        "score": "90",
        "global_rank": "1",
        "primary_recipe": "published_tax_distress",
        "qualification_state": "QUALIFIED_CANDIDATE",
        "dnc_status": "UNKNOWN_NOT_EVALUATED",
    }
    base.update(kwargs)
    return base


def test_prioritize_qualified_then_score_then_rank():
    rows = [
        _estate_row(candidate_id="r", qualification_state="RESEARCH_CANDIDATE", score="99", global_rank="1"),
        _estate_row(candidate_id="q2", qualification_state="QUALIFIED_CANDIDATE", score="10", global_rank="9"),
        _estate_row(candidate_id="q1", qualification_state="QUALIFIED_CANDIDATE", score="80", global_rank="5"),
    ]
    ordered = [r["candidate_id"] for r in prioritize_estate(rows)]
    assert ordered == ["q1", "q2", "r"]


def test_delta_unchanged_changed_new_dropped():
    old = _estate_row()
    kept = _estate_row(candidate_id="keep")
    changed = _estate_row(candidate_id="chg", owner_name="NEW NAME")
    prior_hashes = {
        "keep": identity_tuple_hash(kept),
        "chg": identity_tuple_hash(_estate_row(candidate_id="chg")),
        "gone": identity_tuple_hash(_estate_row(candidate_id="gone")),
    }
    prior_append = [
        {"candidate_id": "keep", "apn": "100", "owner_name": "RANDALL A WOLF", "county": "Wake", "phone": "+19195551212", "confidence_category": "HIGH"},
        {"candidate_id": "chg", "apn": "100", "owner_name": "RANDALL A WOLF", "county": "Wake", "phone": "+19195551213", "confidence_category": "HIGH"},
        {"candidate_id": "gone", "apn": "100", "owner_name": "RANDALL A WOLF", "county": "Wake", "phone": "+19195551214", "confidence_category": "HIGH"},
    ]
    estate = [kept, changed, _estate_row(candidate_id="new")]
    part = partition_delta(
        estate,
        estate_revision="abc",
        prior_append=prior_append,
        prior_hashes=prior_hashes,
        prior_estate_ids=set(prior_hashes),
    )
    assert "keep" in part.unchanged_ids
    assert "chg" in part.changed_ids
    assert "new" in part.new_ids
    assert "gone" in part.dropped_ids
    assert part.rematch_required is True
    assert {r["candidate_id"] for r in part.keep_rows} == {"keep"}
    assert {r["candidate_id"] for r in part.rematch_rows} == {"chg", "new"}


def test_delta_same_revision_keeps_missing_without_rematch():
    estate = [_estate_row(candidate_id="a"), _estate_row(candidate_id="b", owner_name="LLC OWNER LLC")]
    prior_append = [{
        "candidate_id": "a", "apn": "100", "owner_name": "RANDALL A WOLF", "county": "Wake",
        "phone": "+19193311000", "confidence_category": "HIGH",
    }]
    prior_missing = [{"candidate_id": "b", "unresolved_reason": "ENTITY_NEEDS_LAWFUL_SOS_IMPORT"}]
    ids = infer_prior_estate_ids(estate, prior_append, prior_missing)
    part = partition_delta(
        estate,
        estate_revision="rev1",
        prior_append=prior_append,
        prior_hashes={},
        prior_estate_ids=ids,
        prior_estate_revision="rev1",
    )
    assert part.rematch_required is False
    assert part.unchanged_ids == {"a", "b"}
    assert len(part.keep_rows) == 1


def test_every_candidate_classified_and_not_callable():
    estate = [
        _estate_row(),
        _estate_row(candidate_id="c2", owner_name="PDS REAL ESTATE LLC", qualification_state="RESEARCH_CANDIDATE", primary_recipe="heir_property_acquisition_research"),
    ]
    owner_class = {"c1": "LIVING_PERSON", "c2": "LLC"}
    phones = {
        "c1": [{
            "phone": "+19193311000",
            "confidence_category": "HIGH",
            "why_matched": "TIER_A exact core mailing",
            "phone_source": "NCSBE_PUBLIC_VOTER_FILE",
            "phone_role": "OWNER_PERSONAL",
            "matched_person": "RANDALL WOLF",
            "observed_at": "2026-08-16T00:00:00Z",
            "confidence_score": "95",
            "primary_recipe": "published_tax_distress",
            "score": "90",
            "global_rank": "1",
        }],
    }
    ledger = build_resolution_ledger(estate, owner_class, phones, identity_tuple_hash)
    assert len(ledger) == 2
    assert {r["candidate_id"] for r in ledger} == {"c1", "c2"}
    by_id = {r["candidate_id"]: r for r in ledger}
    assert by_id["c1"]["contact_resolution_state"] == PHONE_APPENDED_HIGH
    assert by_id["c2"]["contact_resolution_state"] == "ENTITY_NEEDS_LAWFUL_SOS_IMPORT"
    assert all(r["callability_status"] == CALLABILITY_NOT_AUTHORIZED for r in ledger)
    product = overlay_product_rows(estate, phones, owner_class, {r["candidate_id"]: r["contact_resolution_state"] for r in ledger})
    assert all(r["callability_status"] == "NOT_AUTHORIZED" for r in product)
    assert product[0]["phone_present"] == "YES"
    assert product[1]["phone_present"] == "NO"


def test_contact_retains_who_why_source_recipe_rank():
    rec = {
        "candidate_id": "c1",
        "matched_person": "RANDALL WOLF",
        "why_matched": "TIER_A exact core mailing",
        "phone_source": "NCSBE_PUBLIC_VOTER_FILE",
        "observed_at": "2026-08-16T00:00:00Z",
        "confidence_category": "HIGH",
        "phone_role": "OWNER_PERSONAL",
        "primary_recipe": "published_tax_distress",
        "score": "90",
        "global_rank": "31",
    }
    for field in CONTACT_PROVENANCE_FIELDS:
        assert rec.get(field), field


def test_yield_reports_account_for_every_row_without_pii():
    ledger = [
        {
            "candidate_id": "q1",
            "county": "Wake",
            "fips": "37183",
            "owner_class": "LIVING_PERSON",
            "primary_recipe": "published_tax_distress",
            "qualification_state": "QUALIFIED_CANDIDATE",
            "contact_resolution_state": PHONE_APPENDED_HIGH,
            "phone_present": "YES",
        },
        {
            "candidate_id": "r1",
            "county": "Wake",
            "fips": "37183",
            "owner_class": "LLC",
            "primary_recipe": "published_tax_distress",
            "qualification_state": "RESEARCH_CANDIDATE",
            "contact_resolution_state": "ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
            "phone_present": "NO",
        },
        {
            "candidate_id": "m1",
            "county": "Mecklenburg",
            "fips": "37119",
            "owner_class": "DECEASED",
            "primary_recipe": "heir_property_acquisition_research",
            "qualification_state": "QUALIFIED_CANDIDATE",
            "contact_resolution_state": "DECEASED_OWNER_NEEDS_HEIR_IDENTITY",
            "phone_present": "NO",
        },
    ]
    recipes = recipe_contact_yield(ledger)
    classes = recipe_owner_class_yield(ledger)
    counties = county_free_contact_yield(ledger)
    assert sum(r["candidates"] for r in recipes) == 3
    assert sum(r["phone_appended_high"] for r in recipes) == 1
    assert sum(r["still_missing"] for r in recipes) == 2
    assert sum(r["candidates"] for r in classes) == 3
    assert sum(r["candidates"] for r in counties) == 3
    wake = next(r for r in counties if r["county"] == "Wake")
    assert wake["candidates"] == 2
    assert wake["phone_appended_high"] == 1
    assert yield_columns_are_aggregate(RECIPE_CONTACT_COLUMNS)
    assert yield_columns_are_aggregate(RECIPE_OWNER_CLASS_COLUMNS)
    assert yield_columns_are_aggregate(COUNTY_YIELD_COLUMNS)
    for col in list(RECIPE_CONTACT_COLUMNS) + list(RECIPE_OWNER_CLASS_COLUMNS) + list(COUNTY_YIELD_COLUMNS):
        assert col not in PII_FORBIDDEN_COLUMNS


def test_official_fetch_allowlist_blocks_paid_and_arbitrary_urls():
    assert official_url_allowed("https://s3.amazonaws.com/dl.ncsbe.gov/data/ncvoter60.zip")
    assert official_url_allowed("https://www.irs.gov/pub/irs-soi/eo_nc.csv")
    assert not official_url_allowed("https://api.tracerfy.com/v1/phones")
    assert not official_url_allowed("https://api.batchdata.com/skiptrace")
    assert not official_url_allowed("https://lookups.twilio.com/v2/PhoneNumbers/+19195551212")
    assert not official_url_allowed("http://s3.amazonaws.com/dl.ncsbe.gov/data/ncvoter60.zip")
    assert fetch_official_bytes("https://evil.example/steal") is None


def test_refresh_receipt_is_zero_spend_and_not_production():
    estate = [_estate_row()]
    part = partition_delta(
        estate,
        estate_revision="rev",
        prior_append=[],
        prior_hashes={},
        prior_estate_ids=set(),
    )
    receipt = refresh_receipt(
        part,
        ncsbe_zip_sha256=["abc"],
        high_appended=0,
        held_tier_c=0,
        local_safe_matches=0,
        classified_total=1,
    )
    assert receipt["provider_spend"] == "$0.00"
    assert receipt["production_mutated"] == "NO"
    assert receipt["paid_provider_calls"] == 0
    assert receipt["seller_contact"] == 0
    assert receipt["callability_authorized"] == "NO"
    assert receipt["classified_total"] == 1


def test_phone_present_is_not_callable_claim():
    assert classify_resolution_state(owner_class="LIVING_PERSON", has_high_phone=True) == PHONE_APPENDED_HIGH
    row = overlay_product_rows(
        [_estate_row()],
        {"c1": [{"phone": "+19193311000", "confidence_category": "HIGH", "phone_role": "OWNER_PERSONAL", "phone_source": "NCSBE_PUBLIC_VOTER_FILE"}]},
        {"c1": "LIVING_PERSON"},
        {"c1": PHONE_APPENDED_HIGH},
    )[0]
    assert row["phone_present"] == "YES"
    assert row["callability_status"] == "NOT_AUTHORIZED"


def test_geometry_resolver_still_present_after_reconciliation():
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts" / "resolve_estate_parcel_geometry.py").is_file()
    assert (root / "scripts" / "run_nc_free_phone_enrichment.py").is_file()
    contract = json.loads((root / "contracts" / "free_contact_refresh.v1.json").read_text())
    assert contract["provider_spend"] == "$0.00"
    assert contract["scope"] == "ALL_CURRENT_NC_CANDIDATES"
    assert contract["callability"] == "PHONE_PRESENT_IS_NOT_CALLABLE"


def test_refresh_contract_names_yield_files():
    text = (Path(__file__).resolve().parents[1] / "docs" / "FREE_CONTACT_REFRESH_CONTRACT.md").read_text()
    for name in (
        "RECIPE_CONTACT_YIELD.csv",
        "RECIPE_OWNER_CLASS_YIELD.csv",
        "COUNTY_FREE_CONTACT_YIELD.csv",
        "CURRENT_STILL_MISSING_PHONE.csv",
    ):
        assert name in text
    assert "PHONE PRESENT ≠ CALLABLE" in text or "PHONE PRESENT != CALLABLE" in text


# --- T20C completion: source custody, actionable queue, recipe intelligence ---

def test_source_custody_is_cache_first_and_records_drift(tmp_path):
    from app.contact.official_fetch import (
        drifted_sources,
        ensure_official_source,
        load_custody,
        sha256_file,
        write_custody,
    )

    src = tmp_path / "eo_nc.csv"
    src.write_text("EIN,NAME\n123,ACME\n")
    custody: dict = {}

    first = ensure_official_source(src, "https://www.irs.gov/pub/irs-soi/eo_nc.csv",
                                   custody=custody, family="IRS_EO_ICO_NCSBE")
    assert first["state"] == "CACHED_UNPINNED"
    assert first["downloaded"] is False
    assert first["sha256"] == sha256_file(src)

    # Pinned and unchanged: reused as-is, never re-downloaded.
    second = ensure_official_source(src, "https://www.irs.gov/pub/irs-soi/eo_nc.csv",
                                    custody=custody, family="IRS_EO_ICO_NCSBE")
    assert second["state"] == "CACHED_UNCHANGED"
    assert second["downloaded"] is False
    assert not drifted_sources(custody)

    # The input changed underneath us: recorded, not silently adopted.
    src.write_text("EIN,NAME\n123,ACME\n456,OTHER\n")
    third = ensure_official_source(src, "https://www.irs.gov/pub/irs-soi/eo_nc.csv",
                                   custody=custody, family="IRS_EO_ICO_NCSBE")
    assert third["state"] == "SOURCE_DRIFT"
    assert third["prior_sha256"] == second["sha256"]
    assert drifted_sources(custody) == ["eo_nc.csv"]

    receipt = write_custody(tmp_path / "free_source_custody.json", custody)
    assert json.loads(receipt.read_text())["provider_spend"] == "$0.00"
    assert load_custody(receipt)["eo_nc.csv"]["state"] == "SOURCE_DRIFT"


def test_source_custody_never_downloads_a_paid_provider(tmp_path):
    from app.contact.official_fetch import ensure_official_source

    custody: dict = {}
    rec = ensure_official_source(tmp_path / "skiptrace.json",
                                 "https://api.batchdata.com/skiptrace",
                                 custody=custody, family="PAID")
    assert rec["state"] == "REFUSED_NOT_OFFICIAL"
    assert rec["downloaded"] is False
    assert not (tmp_path / "skiptrace.json").exists()


def test_missing_queue_is_actionable(tmp_path):
    from app.contact.product_export import (
        MISSING_COLUMNS,
        next_safe_free_route,
        write_product_suite,
    )

    written = []

    def _write_csv(path, rows, columns):
        written.append((Path(path).name, list(rows), tuple(columns)))
        return {"path": str(path), "rows": len(rows)}

    estate = [
        _estate_row(candidate_id="e1", owner_name="PDS REAL ESTATE LLC"),
        _estate_row(candidate_id="d1", owner_name="JOHN DOE HEIRS OF"),
    ]
    owner_class = {"e1": "LLC", "d1": "HEIRS"}
    write_product_suite(
        estate, {}, owner_class, set(), tmp_path, _write_csv,
        resolution_by_cand={"e1": "ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
                            "d1": "HEIR_ON_ROLL_NO_ACCEPTED_PHONE"},
        attempted_default="NCSBE_PUBLIC_VOTER_FILE;IRS_EO_ICO_NCSBE",
        last_attempted_at="2026-08-19T10:00:00Z",
    )
    missing = next(rows for name, rows, _ in written if name == "CURRENT_STILL_MISSING_PHONE.csv")
    assert len(missing) == 2
    for row in missing:
        for col in ("resolution_state", "unresolved_reason", "attempted_sources",
                    "last_attempted_at", "next_safe_free_route"):
            assert col in MISSING_COLUMNS
            assert str(row[col]).strip(), (col, row["candidate_id"])
    by_id = {r["candidate_id"]: r for r in missing}
    assert "SOS_IMPORT" in by_id["e1"]["next_safe_free_route"]
    assert by_id["d1"]["resolution_state"] == "HEIR_ON_ROLL_NO_ACCEPTED_PHONE"
    # Government is terminal: no free route is an honest answer, not a bug.
    assert next_safe_free_route("GOVERNMENT_NOT_A_SELLER_CONTACT") == ""


def test_yield_dimensions_separate_held_from_unresolved():
    from app.contact.yield_reports import recipe_contact_yield

    ledger = [
        {"candidate_id": "a", "county": "Wake", "owner_class": "LIVING_PERSON",
         "primary_recipe": "r1", "qualification_state": "QUALIFIED_CANDIDATE",
         "contact_resolution_state": PHONE_APPENDED_HIGH, "phone_present": "YES"},
        {"candidate_id": "b", "county": "Wake", "owner_class": "LIVING_PERSON",
         "primary_recipe": "r1", "qualification_state": "QUALIFIED_CANDIDATE",
         "contact_resolution_state": "IDENTITY_MATCHED_VOTER_PHONE_ABSENT", "phone_present": "NO"},
        {"candidate_id": "c", "county": "Wake", "owner_class": "LIVING_PERSON",
         "primary_recipe": "r1", "qualification_state": "RESEARCH_CANDIDATE",
         "contact_resolution_state": "TIER_C_HELD_NOT_APPENDED", "phone_present": "NO"},
        {"candidate_id": "d", "county": "Wake", "owner_class": "LLC",
         "primary_recipe": "r1", "qualification_state": "RESEARCH_CANDIDATE",
         "contact_resolution_state": "ENTITY_NEEDS_LAWFUL_SOS_IMPORT", "phone_present": "NO"},
    ]
    row = recipe_contact_yield(ledger)[0]
    assert row["candidates"] == 4
    assert row["identity_resolved"] == 2      # appended + phone-absent identity
    assert row["phone_appended_high"] == 1
    assert row["review_held"] == 1            # TIER_C held is not "unresolved"
    assert row["unresolved"] == 1             # only the entity
    assert row["still_missing"] == 3
    assert row["phone_fill_rate"] == "0.250000"
    assert row["high_confidence_rate"] == "0.500000"  # 1 of 2 identity-resolved
    assert row["counties"] == "Wake"
    assert row["no_identity_match"] == 0


def test_recipe_intelligence_is_analytical_not_a_score_change():
    from app.contact.yield_reports import (
        recipe_contact_yield,
        recipe_intelligence,
        recipe_owner_class_yield,
    )

    ledger = []
    for i in range(40):
        ledger.append({
            "candidate_id": f"g{i}", "county": "Wake", "owner_class": "LIVING_PERSON",
            "primary_recipe": "good_recipe", "qualification_state": "QUALIFIED_CANDIDATE",
            "contact_resolution_state": PHONE_APPENDED_HIGH if i < 30 else "UNRESOLVED",
            "phone_present": "YES" if i < 30 else "NO", "score": "90",
        })
    for i in range(40):
        ledger.append({
            "candidate_id": f"b{i}", "county": "Mecklenburg", "owner_class": "LLC",
            "primary_recipe": "bad_recipe", "qualification_state": "RESEARCH_CANDIDATE",
            "contact_resolution_state": "ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
            "phone_present": "NO", "score": "90",
        })
    for i in range(30):
        ledger.append({
            "candidate_id": f"h{i}", "county": "Durham", "owner_class": "HEIRS",
            "primary_recipe": "heir_recipe", "qualification_state": "QUALIFIED_CANDIDATE",
            "contact_resolution_state": "HEIR_ON_ROLL_NO_ACCEPTED_PHONE",
            "phone_present": "NO", "score": "90",
        })

    recipes = recipe_contact_yield(ledger)
    classes = recipe_owner_class_yield(ledger)
    intel = recipe_intelligence(recipes, classes)

    assert intel["TOP_RECIPE_BY_FREE_PHONE_YIELD"].startswith("good_recipe")
    assert intel["TOP_RECIPE_BY_HIGH_CONFIDENCE_RATE"].startswith("good_recipe")
    assert intel["LOWEST_RECIPE_BY_FREE_PHONE_YIELD"].startswith(("bad_recipe", "heir_recipe"))
    assert intel["HIGHEST_HEIRS_RECIPE"].startswith("heir_recipe")
    assert intel["HIGHEST_ENTITY_RECIPE"].startswith("bad_recipe")
    assert intel["RECIPE_COUNT"] == 3
    # Scores are untouched by contact outcomes.
    assert {r["score"] for r in ledger} == {"90"}


def test_receipt_separates_reused_from_newly_appended():
    estate = [_estate_row(candidate_id="a"), _estate_row(candidate_id="b")]
    part = partition_delta(estate, estate_revision="rev", prior_append=[],
                           prior_hashes={}, prior_estate_ids=set())
    receipt = refresh_receipt(
        part, ncsbe_zip_sha256=["abc"], high_appended=5, held_tier_c=2,
        local_safe_matches=0, classified_total=2,
        previous_high_reused=3, new_high_appended=2,
        medium_held=7, review_held=2, source_drift=["ncvoter60.zip"],
    )
    assert receipt["previous_high_reused"] == 3
    assert receipt["new_high_appended"] == 2
    assert receipt["previous_high_reused"] + receipt["new_high_appended"] == receipt["high_appended"]
    assert receipt["medium_held"] == 7
    assert receipt["review_held"] == 2
    assert receipt["source_drift"] == ["ncvoter60.zip"]
    assert receipt["provider_spend"] == "$0.00"


def test_pipeline_doc_answers_the_canonical_questions():
    text = (Path(__file__).resolve().parents[1] / "docs" / "NC_FREE_CONTACT_PIPELINE.md").read_text()
    for heading in (
        "Identity tuple", "Owner routing", "Match tiers", "Confidence", "Fanout",
        "Resolution states", "Free source custody", "Delta refresh",
        "Recipe provenance", "Contact provenance", "Yield reports",
        "Still-missing queue", "Callability boundary", "PII / Git boundary",
        "Replay", "Failure recovery",
    ):
        assert heading in text, heading
    assert "PHONE PRESENT ≠ CALLABLE" in text
    assert "$0.00" in text


def test_machine_contract_versions_every_layer():
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "free_contact_refresh.v1.json").read_text()
    )
    for key in (
        "pipeline", "identity_fields", "source_families", "source_custody",
        "owner_route_classes", "confidence_classes", "resolution_states",
        "delta_rule", "product_outputs", "recipe_provenance_fields",
        "compliance_boundary", "replay",
    ):
        assert contract[key], key
    assert contract["compliance_boundary"]["callability_status"] == "NOT_AUTHORIZED"
    assert contract["compliance_boundary"]["paid_provider_calls"] == 0
    assert contract["compliance_boundary"]["production_contact_rows_imported"] == "NO"
    for field in ("previous_high_reused", "new_high_appended", "medium_held", "review_held"):
        assert field in contract["receipt_fields"], field


def test_no_paid_provider_endpoint_is_reachable_from_contact_code():
    """The $0 invariant, enforced against the source, not against a claim."""
    root = Path(__file__).resolve().parents[1]
    banned = (
        "lookups.twilio.com", "api.twilio.com", "api.batchdata.com",
        "tracerfy.com", "skipforce", "whitepages.com", "api.peopledatalabs",
        "endato.com", "idicore.com",
    )
    targets = list((root / "app" / "contact").glob("*.py"))
    targets.append(root / "scripts" / "run_nc_free_phone_enrichment.py")
    for path in targets:
        text = path.read_text().lower()
        for needle in banned:
            # Named only inside the refusal allowlist test data, never as a call target.
            assert needle not in text, f"{path.name} references paid provider {needle}"


def test_every_report_accounts_for_every_candidate():
    """No row may fall between the buckets — a hold is not an unresolved."""
    from app.contact.yield_reports import (
        county_free_contact_yield,
        recipe_contact_yield,
        recipe_owner_class_yield,
    )

    states = [
        (PHONE_APPENDED_HIGH, "YES", "LIVING_PERSON"),
        ("IDENTITY_MATCHED_VOTER_PHONE_ABSENT", "NO", "LIVING_PERSON"),
        ("TIER_C_HELD_NOT_APPENDED", "NO", "LIVING_PERSON"),
        ("AMBIGUOUS_REJECTED", "NO", "MULTI_PERSON"),
        ("COMMON_NAME_REJECTED", "NO", "LIVING_PERSON"),
        ("NO_ADDRESS_CORROBORATED_NCSBE_MATCH", "NO", "LIVING_PERSON"),
        ("DECEASED_OWNER_NEEDS_HEIR_IDENTITY", "NO", "DECEASED"),
        ("HEIR_ON_ROLL_NO_ACCEPTED_PHONE", "NO", "HEIRS"),
        ("ENTITY_NEEDS_LAWFUL_SOS_IMPORT", "NO", "LLC"),
        ("NONPROFIT_NO_990_PHONE", "NO", "NONPROFIT"),
        ("TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH", "NO", "TRUST"),
        ("GOVERNMENT_NOT_A_SELLER_CONTACT", "NO", "GOVERNMENT"),
        ("UNRESOLVED", "NO", "UNKNOWN"),
    ]
    ledger = [
        {
            "candidate_id": f"c{i}", "county": "Wake", "fips": "37183",
            "owner_class": oc, "primary_recipe": "r1",
            "qualification_state": "QUALIFIED_CANDIDATE",
            "contact_resolution_state": state, "phone_present": present,
        }
        for i, (state, present, oc) in enumerate(states)
    ]
    buckets = ("identity_resolved", "medium_review", "review_held",
               "no_identity_match", "unresolved")
    for report in (recipe_contact_yield(ledger),
                   recipe_owner_class_yield(ledger),
                   county_free_contact_yield(ledger)):
        assert sum(r["candidates"] for r in report) == len(ledger)
        for row in report:
            assert sum(row[b] for b in buckets) == row["candidates"], row
            assert row["phone_appended_high"] + row["still_missing"] == row["candidates"]
