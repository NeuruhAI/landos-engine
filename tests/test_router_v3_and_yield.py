"""The router must not loop, and must not re-do entity work per parcel."""
from __future__ import annotations

from app.resolution.router import (
    ROUTER_VERSION,
    next_best_source,
    route_with_memory,
)
from app.resolution.source_yield import build_yield, rank_sources


def base(**over):
    kw = dict(owner_class="LIVING_PERSON", owner_condition="LIVING",
              contact_resolution_state="NO_ADDRESS_CORROBORATED_NCSBE_MATCH",
              county="Wake")
    kw.update(over)
    return kw


def test_the_router_is_v3():
    assert ROUTER_VERSION == "next_best_source.v3"


def test_an_automated_source_already_run_is_not_recommended_again():
    """v1 told a candidate that had been through the voter file to try the
    voter file. That is a loop with an operator inside it."""
    first = next_best_source(**base())
    assert first.next_source == "NCSBE_PUBLIC_VOTER_FILE"
    second = next_best_source(**base(attempted_sources=frozenset({"NCSBE_PUBLIC_VOTER_FILE"})))
    assert second.next_source == "MANUAL_PUBLIC_RECORD"
    assert second.queue == "MANUAL_SOURCE_REQUIRED"
    assert "exhausted" in second.blocking_reason


def test_a_manual_route_is_not_demoted_by_having_been_attempted():
    """A human reading a probate file after a script failed is a different act."""
    route = next_best_source(**base(owner_class="HEIRS", owner_condition="HEIRS_UNRESOLVED",
                                    contact_resolution_state="DECEASED_OWNER_NEEDS_HEIR_IDENTITY"))
    assert route.manual_or_automatic == "MANUAL"
    again = route_with_memory(route, frozenset({route.next_source}))
    assert again.next_source == route.next_source


def test_a_resolved_entity_does_not_send_the_operator_back_to_the_sos():
    """One entity owning 89 parcels is one lookup, not 89."""
    route = next_best_source(**base(owner_class="LLC", owner_condition="ENTITY_ACTIVE",
                                    contact_resolution_state="ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
                                    entity_resolution_state="ENTITY_PRINCIPAL_FOUND",
                                    entity_principal_known=True))
    assert route.next_source == "OPERATOR_CONTACT_ENTITY_PRINCIPAL"
    assert route.queue == "ENTITY_PRINCIPAL_FOUND_CONTACT_NEEDED"


def test_an_entity_with_only_an_address_asks_for_the_registry_not_a_new_search():
    route = next_best_source(**base(owner_class="LLC", owner_condition="ENTITY_ACTIVE",
                                    contact_resolution_state="ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
                                    entity_resolution_state="ENTITY_OFFICIAL_ADDRESS_FOUND"))
    assert route.queue == "ENTITY_IDENTITY_FOUND_CONTACT_NEEDED"


def test_an_unresolved_entity_still_takes_the_original_route():
    route = next_best_source(**base(owner_class="LLC", owner_condition="ENTITY_ACTIVE",
                                    contact_resolution_state="ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
                                    entity_resolution_state="ENTITY_IDENTITY_REQUIRED"))
    assert route.queue == "SOS_ENTITY_IDENTITY_REQUIRED"


def test_an_existing_high_contact_is_still_the_first_answer():
    route = next_best_source(**base(has_high_phone=True))
    assert route.queue == "CONTACT_FOUND_HIGH"


def test_no_route_ever_carries_a_cost():
    for kw in (base(), base(owner_class="TRUST", owner_condition="TRUST_HELD"),
               base(owner_class="GOVERNMENT", owner_condition="GOVERNMENT"),
               base(tier_c_held=True)):
        assert next_best_source(**kw).cost_usd == "0.00"


def test_yield_counts_by_every_cut_the_router_uses():
    rows = [
        {"source": "NCSBE", "owner_class": "LIVING_PERSON", "county": "Wake",
         "recipe": "r1", "resolution_stage": "IDENTITY", "identity_resolved": True,
         "contact_resolved": False, "confidence": "HIGH", "manual": False},
        {"source": "NCSBE", "owner_class": "LIVING_PERSON", "county": "Mecklenburg",
         "recipe": "r1", "resolution_stage": "IDENTITY", "identity_resolved": True,
         "contact_resolved": True, "confidence": "HIGH", "manual": False},
        {"source": "NCSBE", "owner_class": "LIVING_PERSON", "county": "Wake",
         "recipe": "r1", "resolution_stage": "IDENTITY", "identity_resolved": False,
         "contact_resolved": False, "confidence": "", "manual": False},
    ]
    out = build_yield(rows)
    wake = [r for r in out if r["dimension"] == "county" and r["key"] == "Wake"][0]
    assert wake["attempts"] == 2
    assert wake["identities_resolved"] == 1
    assert wake["contacts_resolved"] == 0
    assert wake["identity_rate"] == 0.5
    meck = [r for r in out if r["dimension"] == "county" and r["key"] == "Mecklenburg"][0]
    assert meck["contact_rate"] == 1.0


def test_a_single_lucky_hit_does_not_outrank_a_hundred_attempts():
    rows = ([{"source": "LUCKY", "owner_class": "X", "county": "C", "recipe": "r",
              "resolution_stage": "s", "identity_resolved": True, "contact_resolved": True,
              "confidence": "HIGH", "manual": False}]
            + [{"source": "STEADY", "owner_class": "X", "county": "C", "recipe": "r",
                "resolution_stage": "s", "identity_resolved": True, "contact_resolved": True,
                "confidence": "HIGH", "manual": False} for _ in range(100)])
    ranked = rank_sources(build_yield(rows), "county", "C")
    assert [r["source"] for r in ranked] == ["STEADY", "LUCKY"]


def test_every_queue_the_router_can_reach_has_a_contact_status():
    """The refresh writes QUEUE_CONTACT_STATUS[queue] as the panel's status.
    A queue the router can emit but the vocabulary does not name lands as a
    blank status on every row routed there."""
    from app.resolution import router as r

    reachable = {
        getattr(r, name) for name in dir(r)
        if name.startswith("Q_") and isinstance(getattr(r, name), str)
    }
    missing = {q for q in reachable if not r.QUEUE_CONTACT_STATUS.get(q)}
    assert missing == set()
