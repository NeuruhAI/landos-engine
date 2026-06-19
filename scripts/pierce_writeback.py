"""
pierce_writeback.py — ENTITY PIERCE BATCH writeback (D1 v2 SOP-2 layer, engine-side).

    python -m scripts.pierce_writeback /tmp/pierce_results_accum.json

Takes the registry-pierce results collected from the MiBusiness Registry Portal
(MI LARA's COFS successor) and writes them into landos.owner_leads.skip_trace_meta
for the qualified entity rows. WRITE SURFACE: landos.owner_leads ONLY — never
public.*, never skip_trace_status / confidence_score (a pierce is not a phone;
P3's importer owns those).

Per row outcome:
  REGISTRY HIT   -> outreach_recipe=R2, primary_target=member,
                    controlling_human {name, role, source} (president from the
                    officer slate > human resident agent > affiliated entity
                    agent > null when only a commercial agent is exposed),
                    entity_record {agent, commercial_agent_flag, principal_addr,
                    filing_id, names, status, jurisdiction, management_type,
                    officers}, top-level commercial_agent_flag.
  TRUST          -> outreach_recipe=R3, primary_target=trustee, no registry —
                    deed/mailing-reverse pierce is the VA path (runbook SOP-2.6).
  MISS           -> provenance note only (pierce_attempt + origin-state branch);
                    recipe NOT stamped — no fabricated piercing.
  REJECTED MATCH -> recorded as a miss with the rejection reason (honesty rule:
                    a wrong match is worse than no match).

MERGE, never clobber: only the pierce keys are overlaid; phones/notes/etc.
from other write paths survive. Idempotent on lead_id — re-runs converge.

Ends with PORTFOLIO DETECTION: dedupes controlling-human names + principal
addresses across all pierced rows and prints any HUB controlling 2+ parcels.
"""
import json
import re
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
LOOKUP_STAMP = "2026-06-09/10 single-lookup batch, mibusinessregistry.lara.state.mi.us"

COMMERCIAL_AGENTS = [
    "CT CORPORATION", "C T CORPORATION", "THE CORPORATION COMPANY", "CSC",
    "CSC-LAWYERS", "REGISTERED AGENTS INC", "INCORP SERVICES",
    "NORTHWEST REGISTERED AGENT", "CAPITOL CORPORATE SERVICES",
    "BUSINESS FILINGS INCORPORATED", "COGENCY", "UNITED AGENT GROUP",
    "SOUTHWEST MICHIGAN REGISTERED AGENTS",
]

# retry-query -> the tax-roll owner_name it answers for
RETRY_MAP = {
    "WALTERS-DIMMICK PETROLEUM": "WALTERS-DIMMICK PETRO INC",
    "LANDMARK PROPERTIES": "LANDMARK PROPERTIES REDEV LLC",
    "MARTIN BROTHERS": "MARTIN BROTHERS MIL & FOUNDR",
    "M C YOUNG": "YOUNG M C INC",
}
REJECTED = {
    "MARTIN BROTHERS": ("matched 'MARTIN BROTHERS DISTRIBUTING COMPANY, INC.' "
                        "(Iowa food distributor) from a truncated foundry name — "
                        "business type inconsistent, match REJECTED; VA verifies "
                        "the real 'Martin Brothers Mill & Foundry' via deeds"),
}
LOW_CONFIDENCE = {
    "WALTERS-DIMMICK PETROLEUM": ("2 registry candidates; matched record is the "
                                  "INDIANA sibling that ceased by merger 09/1986 — "
                                  "the surviving 'WALTERS-DIMMICK PETROLEUM, INC.' "
                                  "(MI domestic) is the likely live owner; VA verifies"),
}
PERSON_PAIR = {"HADLEY ROSS F C & HADLEY PEGGY JO", "WITHROW LARRY & WITHROW VIRGINIA"}
SPECIAL_MISS_NOTE = {
    "CHEMICAL BANK FACILITIES": ("defunct bank brand (Chemical Bank -> TCF -> "
                                 "Huntington 2021/22); no MI registry record under "
                                 "this name — route via Huntington corporate real "
                                 "estate, not people-search"),
    "Vantage Options, LLC": ("Indiana entity (county 18141, Mishawaka IN mailing) — "
                             "origin registry is INBiz (bsd.sos.in.gov), not searched "
                             "this run (INBiz UI not yet integrated); VA/next pass"),
}
ORIGIN_HINT = {"CA": "bizfileonline.sos.ca.gov (API token-gated; UI manual)",
               "WA": "ccfs.sos.wa.gov", "FL": "search.sunbiz.org",
               "IN": "bsd.sos.in.gov/publicbusinesssearch"}


def parse_officer(v):
    """'NAME   ADDR' (3-space sep) -> (name, addr)."""
    if not v:
        return None, None
    parts = re.split(r"\s{3,}", v.strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)


def is_commercial(agent):
    a = (agent or "").upper()
    return any(c in a for c in COMMERCIAL_AGENTS)


def looks_human(name):
    n = (name or "").upper()
    return n and not re.search(r"\b(LLC|L\.L\.C|INC|CORP|COMPANY|SERVICES?|FILINGS)\b", n)


def build_meta_patch(res):
    row, det = res["row"], res.get("detail") or {}
    agent = det.get("Resident Agent Name") or row.get("agent") or ""
    commercial = is_commercial(agent)

    officers = {}
    for label in ("President", "Secretary", "Treasurer"):
        nm, addr = parse_officer(det.get(f"{label} Name & Address"))
        if nm:
            officers[label.lower()] = {"name": nm, "addr": addr}

    ch_name, ch_role = None, None
    if "president" in officers:
        ch_name, ch_role = officers["president"]["name"], "president (registry officer slate)"
    elif not commercial and looks_human(agent):
        mt = (det.get("Management Type") or "").lower()
        ch_role = f"{mt or 'principal'} (resident agent, human)" if mt != "manager" \
            else "manager (resident agent, human)"
        ch_name = agent
    elif not commercial and agent:
        ch_name, ch_role = agent, "resident agent (affiliated entity, not commercial)"
    src = (f"{LOOKUP_STAMP}: entity {det.get('Identification #') or row.get('entity_id')}"
           f" ({det.get('Jurisdiction', '?')}, {det.get('Entity Status') or row.get('status')})")
    q = res["q"]
    if q in LOW_CONFIDENCE:
        src += f" — LOW-CONFIDENCE MATCH: {LOW_CONFIDENCE[q]}"

    entity_record = {
        "agent": agent + (" (COMMERCIAL AGENT - flagged, skipped)" if commercial else ""),
        "commercial_agent_flag": commercial,
        "members": [],
        "officers": officers or None,
        "principal_addr": det.get("Registered Office Mailing Address")
                          or det.get("Registered Office Street Address"),
        "registered_office_street": det.get("Registered Office Street Address"),
        "source": LOOKUP_STAMP,
        "filing_id": det.get("Identification #") or row.get("entity_id"),
        "current_name": det.get("Entity Name") or (row.get("title") or [None])[0],
        "prior_names": [p.strip() for p in (det.get("Prior Names") or "").split(",") if p.strip()],
        "status": det.get("Entity Status") or row.get("status"),
        "ar_standing": det.get("AR Standing"),
        "jurisdiction": det.get("Jurisdiction"),
        "management_type": det.get("Management Type"),
        "inactive_date": det.get("Inactive Date"),
    }
    return {
        "outreach_recipe": "R2",
        "primary_target": "member",
        "commercial_agent_flag": commercial,
        "controlling_human": {"name": ch_name, "role": ch_role, "source": src},
        "entity_record": entity_record,
    }


def main() -> None:
    results = json.load(open(sys.argv[1]))
    by_owner = {}
    for r in results:
        owner = RETRY_MAP.get(r["q"], r["q"]).upper()
        if r["q"] in REJECTED:
            by_owner[owner] = {"rejected": REJECTED[r["q"]], "q": r["q"]}
        elif "row" in r:
            by_owner[owner] = r            # hit (retry hits overwrite the miss)
        elif owner not in by_owner:
            by_owner[owner] = r            # miss (keep unless a hit exists)

    counts = defaultdict(int)
    pierced_rows = []   # (owner_name, lead_id, ch_name, principal_addr, status)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select lead_id, owner_name, owner_mailing_state, skip_trace_meta
               from landos.owner_leads
               where tenant_id=%s and not discovery_seed and owner_type='entity'
               order by final_lead_score desc nulls last""", (TENANT,))
        leads = cur.fetchall()

        for lead in leads:
            name_u = lead["owner_name"].upper()
            meta = dict(lead["skip_trace_meta"] or {})
            if meta.get("entity_record") and "ALUTECH" in name_u:
                counts["already_pierced (P2)"] += 1
                continue

            patch, outcome = None, None
            if re.search(r"\bTRUST\b", name_u):
                outcome = "trust -> R3 (deed path)"
                patch = {
                    "outreach_recipe": "R3", "primary_target": "trustee",
                    "entity_record": {
                        "status": "trust — no public registry",
                        "source": LOOKUP_STAMP,
                        "pierce_path": "deed / mailing-address reverse search (VA, runbook SOP-2.6)",
                    },
                }
            else:
                res = by_owner.get(name_u)
                if res is None:
                    outcome = "not in batch"
                    counts[outcome] += 1
                    continue
                if "rejected" in res:
                    outcome = "match rejected -> miss"
                    patch = {"entity_record": {
                        "status": "registry match REJECTED",
                        "source": LOOKUP_STAMP, "note": res["rejected"]}}
                elif "row" in res:
                    outcome = "pierced (R2)"
                    patch = build_meta_patch(res)
                else:
                    note = SPECIAL_MISS_NOTE.get(lead["owner_name"]) or \
                           SPECIAL_MISS_NOTE.get(res["q"])
                    if name_u in PERSON_PAIR:
                        outcome = "misclassified person-pair"
                        note = ("not an entity — married-couple tax-roll name; "
                                "owner_type misclassification; route SOP-1 person branch")
                    else:
                        outcome = "no MI registration (foreign-origin branch)"
                        st = lead["owner_mailing_state"]
                        note = note or (f"no MI record; mailing state {st} -> origin "
                                        f"registry {ORIGIN_HINT.get(st, st + ' SOS')}")
                    patch = {"entity_record": {
                        "status": "no_registration_found_mi",
                        "source": LOOKUP_STAMP, "origin_state_branch": note}}

            counts[outcome] += 1
            merged = {**meta, **patch}
            cur.execute(
                """update landos.owner_leads set skip_trace_meta=%s, updated_at=now()
                   where tenant_id=%s and lead_id=%s""",
                (Jsonb(merged), TENANT, lead["lead_id"]))
            if outcome == "pierced (R2)":
                er = patch["entity_record"]
                pierced_rows.append((lead["owner_name"], str(lead["lead_id"]),
                                     patch["controlling_human"]["name"],
                                     er.get("principal_addr"), er.get("status")))
            print(f"  {lead['owner_name'][:36]:<36} {outcome}")
        conn.commit()

    print("\n=== writeback counts ===")
    for k, v in sorted(counts.items()):
        print(f"  {k:<38} {v}")

    # ---- PORTFOLIO DETECTION -------------------------------------------------
    def norm_name(s):
        return re.sub(r"[^A-Z ]", "", (s or "").upper()).replace("  ", " ").strip()

    def norm_addr(s):
        s = re.sub(r"[^A-Z0-9 ]", "", (s or "").upper())
        s = re.sub(r"\b(STE|SUITE|STREET|ST|AVE|AVENUE|RD|ROAD|DR|DRIVE)\b", "", s)
        return re.sub(r"\s+", " ", s).strip()

    hubs_by_name, hubs_by_addr = defaultdict(list), defaultdict(list)
    for owner, lid, ch, addr, status in pierced_rows:
        if ch and norm_name(ch):
            hubs_by_name[norm_name(ch)].append(owner)
        if addr and norm_addr(addr):
            hubs_by_addr[norm_addr(addr)].append(owner)
    # Aludyne (P2 pierce) joins the scan
    hubs_by_name[norm_name("Aludyne, Inc.")].append("ALUTECH LLC")

    print("\n=== PORTFOLIO HUBS (controlling human/name across 2+ parcels) ===")
    for nm, owners in sorted(hubs_by_name.items(), key=lambda kv: -len(kv[1])):
        if len(owners) > 1:
            print(f"  {nm:<28} x{len(owners)}: {', '.join(sorted(set(owners)))[:120]}")
    print("=== PORTFOLIO HUBS (shared principal address) ===")
    for ad, owners in sorted(hubs_by_addr.items(), key=lambda kv: -len(kv[1])):
        if len(owners) > 1:
            print(f"  {ad[:44]:<44} x{len(owners)}: {', '.join(sorted(set(owners)))[:110]}")


if __name__ == "__main__":
    main()
