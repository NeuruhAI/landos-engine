"""Invented routes I1–I5 on top of the estate itself."""
from __future__ import annotations

from typing import Dict, List, Optional

from .attempts import Attempt
from .estate_rows import is_entity_owner, is_out_of_state, is_probate
from .human_routes import t5_sos_search, t7_rod_search, t10_people_search
from .receipts import RouteReceipt
from .routing_order import route_order_for

STATE_RECORD_HINT = {
    "FL": "county property appraiser + clerk of court official records", "GA": "GSCCCA (gsccca.org) deed index", "SC": "county register of deeds",
    "VA": "circuit court land records", "TX": "county clerk official public records", "NY": "ACRIS (NYC) / county clerk", "CA": "county recorder",
}


def i1_out_of_state(estate_rows: List[dict], *, run_id: str, now_iso: str, receipt_path: Optional[str]) -> Dict:
    """Owner mailing state ≠ NC is a route of its own: their home-state records, as one-click hints."""
    rows = [r for r in estate_rows if is_out_of_state(r)]
    worklist = []; attempts = []
    for r in rows:
        st = (r.get("mailing_state") or "").strip().upper()
        worklist.append({"route_id": "I1", "global_rank": r.get("global_rank", ""), "candidate_id": r["candidate_id"], "name": r.get("owner_name", ""),
                         "role": "recorded_owner", "county": r.get("county", ""), "situs_address": r.get("property_address_raw", ""), "apn": r.get("apn", ""),
                         "url": "", "instructions": f"Owner mails to {st}: search {STATE_RECORD_HINT.get(st, st + ' county recorder / assessor')} for '{r.get('owner_name','')}' at '{r.get('mailing_address','')}, {r.get('mailing_city','')} {st} {r.get('mailing_zip','')}'.",
                         "paste_back": "home-state deed / assessor mailing address confirmation, phone if published"})
        attempts.append(Attempt(r["candidate_id"], "I1", run_id, "HIT", "ESTATE_MAILING_STATE", None, now_iso, receipt_path, {"mailing_state": st}))
    receipt = RouteReceipt("I1", "Out-of-state router", "AUTOMATED", "the estate's own mailing state", "Internal data.", "scripts/export_opportunities.py",
                           rows_tried=len(estate_rows), hits=len(rows), new_contacts=0, new_heirs=0, source_as_of=None, observed_at=now_iso,
                           evidence={"states": _count(r.get("mailing_state", "").upper() for r in rows)})
    return {"worklist": worklist, "attempts": attempts, "receipt": receipt}


I2_COLLAPSE_SQL = """
-- I2: one phone found anywhere attaches to every parcel the same party owns (candidate_parties is the graph).
with party_phone as (
  select cp.party_id, count(distinct pp.phone_e164) as phones
    from public.candidate_parties cp join public.party_phones pp on pp.party_id = cp.party_id and pp.tenant_id = cp.tenant_id
   group by cp.party_id),
parcels as (
  select cp.party_id, count(distinct cp.import_row_id) as parcels from public.candidate_parties cp group by cp.party_id)
select count(*) filter (where p.parcels > 1) as parties_on_many_parcels,
       sum(p.parcels) filter (where p.parcels > 1 and ph.phones > 0) as parcels_reached_by_a_shared_phone,
       count(*) filter (where p.parcels > 1 and ph.phones > 0) as parties_with_phone_and_many_parcels
  from parcels p left join party_phone ph on ph.party_id = p.party_id;
"""


def i3_heir_cross_match(estate_rows: List[dict], heir_parties: List[dict], *, run_id: str, now_iso: str, receipt_path: Optional[str]) -> Dict:
    """heir_parties: [{candidate_id, party_name, role, city}] from candidate_parties (roles HEIR/PR/…).
    Produces the T5/T7/T10 one-click URLs for the HEIR (not the decedent) and marks I3 attempts."""
    by_cand = {r["candidate_id"]: r for r in estate_rows}
    worklist = []; attempts = []
    for h in heir_parties:
        r = by_cand.get(h["candidate_id"], {})
        urls = t10_people_search(h["party_name"], h.get("city") or r.get("property_city", ""))
        rod = t7_rod_search(h["party_name"], r.get("county", ""))
        worklist.append({"route_id": "I3", "global_rank": r.get("global_rank", ""), "candidate_id": h["candidate_id"], "name": h["party_name"], "role": h.get("role", ""),
                         "county": r.get("county", ""), "situs_address": r.get("property_address_raw", ""), "apn": r.get("apn", ""), "url": urls["truepeoplesearch"],
                         "instructions": f"Run the free routes on the heir, not the decedent: voter file match (T6, automated), ROD {rod['url'] or '(no online search)'}, people search {urls['google']}.",
                         "paste_back": "heir phone / mailing address as read (confidence = operator_web)"})
        attempts.append(Attempt(h["candidate_id"], "I3", run_id, "HIT", "CANDIDATE_PARTIES_HEIR", None, now_iso, receipt_path, {"party": h["party_name"], "role": h.get("role")}))
    receipt = RouteReceipt("I3", "Heir-name cross-match", "AUTOMATED", "candidate_parties (heir / PR / executor / administrator / trustee)", "Internal data; the downstream routes keep their own terms.",
                           "internal", rows_tried=len(heir_parties), hits=len(heir_parties), new_contacts=0, new_heirs=0, source_as_of=None, observed_at=now_iso)
    return {"worklist": worklist, "attempts": attempts, "receipt": receipt}


def i2_identity_collapse(conn, *, now_iso: str, receipt_path: Optional[str]) -> Dict:
    """I2: how far one phone reaches through the party graph. Read-only."""
    from .receipts import RouteReceipt
    with conn.cursor() as cur:
        cur.execute(I2_COLLAPSE_SQL)
        row = cur.fetchone()
    many, reached, with_phone = (row[0] or 0, row[1] or 0, row[2] or 0) if row else (0, 0, 0)
    return {"receipt": RouteReceipt("I2", "Identity graph collapse (same person across parcels)", "AUTOMATED",
                                    "public.candidate_parties / party_phones", "Internal data.", "migration 104 party model",
                                    rows_tried=int(many), hits=int(with_phone), new_contacts=int(reached), new_heirs=0,
                                    source_as_of=None, observed_at=now_iso,
                                    evidence={"parties_on_many_parcels": int(many), "parcels_reached_by_a_shared_phone": int(reached),
                                              "parties_with_phone_and_many_parcels": int(with_phone)})}


HEIR_PARTIES_SQL = """
select cp.import_row_id::text, p.canonical_name, cp.relationship_role
  from public.candidate_parties cp join public.parties p on p.party_id = cp.party_id
 where cp.relationship_role in ('HEIR','PERSONAL_REPRESENTATIVE','EXECUTOR','ADMINISTRATOR','TRUSTEE')
"""

WATCHES_SQL = "select count(*) from public.deedsonar_watches where coalesce(state, 'active') not in ('archived', 'deleted')"


def i3_heir_parties_from_db(conn, estate_rows: List[dict]) -> List[dict]:
    """The heir / PR parties already linked to a candidate, keyed by the estate's candidate_id."""
    ids = {r["candidate_id"] for r in estate_rows}
    with conn.cursor() as cur:
        cur.execute(HEIR_PARTIES_SQL)
        rows = cur.fetchall()
    return [{"candidate_id": r[0], "party_name": r[1], "role": r[2]} for r in rows if r[0] in ids]


def i5_refire_watches(conn, *, now_iso: str, receipt_path: Optional[str]) -> Dict:
    """I5: every route run re-fires the delta engine; the receipt records how many watches will see it."""
    from .receipts import RouteReceipt
    with conn.cursor() as cur:
        cur.execute(WATCHES_SQL)
        n = (cur.fetchone() or [0])[0] or 0
    return {"receipt": RouteReceipt("I5", "Watches / deltas re-fire after every route run", "AUTOMATED",
                                    "public.deedsonar_watches (delta engine)", "Internal.", "/api/cron/watches",
                                    rows_tried=int(n), hits=int(n), new_contacts=0, new_heirs=0, source_as_of=None,
                                    observed_at=now_iso, notes=["the next /api/cron/watches tick re-evaluates these watches against the new data"])}


def i4_route_orders(estate_rows: List[dict]) -> Dict[str, List[str]]:
    out = {}
    for r in estate_rows:
        comps = [r.get("primary_recipe", ""), r.get("qualification_state", "")]
        out[r["candidate_id"]] = route_order_for(comps, r.get("owner_condition", "") or ("DECEASED" if is_probate(r) else ""), "ENTITY" if is_entity_owner(r) else "")
    return out


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v or "(blank)"] = out.get(v or "(blank)", 0) + 1
    return out
