"""Per-target executors for the free contact routes whose record lives in the DeedSonar spine.

T6 (NCSBE voter file), I2 (identity graph) and I3 (heir cross-match) already run as lanes
over the estate (app/free_routes: the T6 NCSBE lane, invented.i2 / invented.i3). They write
their per-row outcome to public.free_route_attempts and their evidence to candidate_parties /
party_phones / contact_evidence (project_contacts_to_deedsonar). A free-first job asks that
SAME record about its one target -- it never re-implements a matcher:

  T6  the lane's newest attempt for the parcel's estate row(s). HIT: the NCSBE-sourced phone
      evidence. NO_HIT: the lane's own resolution (e.g. DECEASED_OWNER_NEEDS_HEIR_IDENTITY).
      Dated by the voter file (source_as_of). No attempt for the parcel = UNWIRED: the lane
      has not covered it, so the free path stays open. T6 persists phones, not voter mailing
      addresses, so for a mailing fact it is UNWIRED rather than a false NO_HIT.
  I2  every phone of every party linked to the parcel. party_phones are party-level, so a
      phone found on any parcel the same party owns reaches this one.
  I3  phones of the heir / personal representative / executor / administrator / trustee
      parties linked to the parcel; NOT_APPLICABLE when there are none.

The spine connection arrives already resolved through the contact authority and set
read-only (app/free_first/connections.py); this module never opens a connection itself.
Evidence carries a masked phone and the spine ids (party_phone_id, contact_evidence_id);
the number itself stays in contact custody.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from app.free_routes.attempts import TENANT as DEEDSONAR_TENANT

from .ledger import JobState
from .provenance import make_evidence, sha256_hex, utc_now_iso
from .runner import AttemptResult, Executor, parcel_tier_executor

HEIR_ROLES = ("HEIR", "PERSONAL_REPRESENTATIVE", "EXECUTOR", "ADMINISTRATOR", "TRUSTEE")
_CONFIDENCE = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}

ESTATE_ROWS_SQL = """
select row_id::text from public.lead_import_rows
 where tenant_id = %(tenant)s and normalized->>'county_fips' = %(fips)s
   and upper(normalized->>'apn') = upper(%(apn)s)
"""
ROUTE_ATTEMPTS_SQL = """
select outcome, source, source_as_of::text, observed_at, run_id, detail
  from public.free_route_attempts
 where tenant_id = %(tenant)s and route_id = %(route)s and import_row_id = any(%(rows)s::uuid[])
 order by observed_at desc
"""
PARTY_PHONES_SQL = """
select cp.party_id::text, cp.relationship_role, pp.party_phone_id::text, pp.phone_e164, pp.phone_type,
       pp.confidence_category, pp.observed_at, pp.evidence_id, ce.source_family, ce.observed_at, ce.retrieved_at
  from public.candidate_parties cp
  join public.party_phones pp on pp.party_id = cp.party_id and pp.tenant_id = cp.tenant_id
  left join public.contact_evidence ce on ce.evidence_id = pp.evidence_id
 where cp.tenant_id = %(tenant)s and cp.import_row_id = any(%(rows)s::uuid[])
   and coalesce(pp.status, 'active') not in ('retired', 'rejected')
   and (%(roles)s::text[] is null or cp.relationship_role = any(%(roles)s::text[]))
"""
PARTY_COUNT_SQL = """
select count(distinct party_id) from public.candidate_parties
 where tenant_id = %(tenant)s and import_row_id = any(%(rows)s::uuid[])
   and (%(roles)s::text[] is null or relationship_role = any(%(roles)s::text[]))
"""


def mask_phone(e164: Optional[str]) -> str:
    digits = "".join(ch for ch in str(e164 or "") if ch.isdigit())
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)


class SpineRoutes:
    """The spine-backed executors for one research session (estate rows cached per parcel)."""

    def __init__(self, conn, *, tenant: str = DEEDSONAR_TENANT) -> None:
        self.conn = conn
        self.tenant = tenant
        self._rows: Dict[tuple, List[str]] = {}

    def _query(self, sql: str, params: Mapping[str, Any]) -> list:
        with self.conn.cursor() as cur:
            cur.execute(sql, {"tenant": self.tenant, **params})
            return cur.fetchall()

    def estate_rows(self, fips: str, apn: str) -> List[str]:
        key = (fips, str(apn or "").strip().upper())
        if key not in self._rows:
            self._rows[key] = [r[0] for r in self._query(ESTATE_ROWS_SQL, {"fips": fips, "apn": key[1]})] if key[1] else []
        return self._rows[key]

    def _phone_evidence(self, rows: List[str], *, roles, source_family: Optional[str], source_effective_date,
                        route: str) -> list:
        out = []
        for (party_id, role, pp_id, e164, ptype, conf, pp_observed, ev_id, fam, ev_observed,
             ev_retrieved) in self._query(PARTY_PHONES_SQL, {"rows": rows, "roles": list(roles) if roles else None}):
            if source_family and fam != source_family:
                continue
            observed = _iso(ev_observed or pp_observed)
            out.append(make_evidence(
                fact="contact.phone", source_id=f"deedsonar.party_phones:{route}",
                value={"phone_masked": mask_phone(e164), "phone_sha256": sha256_hex(str(e164)), "phone_type": ptype,
                       "party_id": party_id, "role": role, "party_phone_id": pp_id, "contact_evidence_id": ev_id},
                retrieved_at=utc_now_iso(), confidence=_CONFIDENCE.get(str(conf or "").upper(), "MEDIUM"),
                locator=f"efxo:public.party_phones/{pp_id}", raw_payload={"party_phone_id": pp_id, "evidence_id": ev_id},
                observed_at=observed, source_effective_date=source_effective_date or (observed or "")[:10] or None,
                source_record_id=ev_id or pp_id))
        return out

    # ------------------------------------------------------------------ routes
    def t6(self, step: Mapping[str, Any], state: JobState) -> AttemptResult:
        if state.fact != "contact.phone":
            return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                   "message": "the T6 lane persists phones, not voter mailing addresses"})
        rows = self.estate_rows(state.county_fips, state.opened["target"].get("apn"))
        if not rows:
            return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                   "message": "parcel is not in the estate the T6 lane covers"})
        attempts = self._query(ROUTE_ATTEMPTS_SQL, {"route": "T6", "rows": rows})
        if not attempts:
            return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                   "message": "the T6 lane has not run for this parcel"})
        outcome, source, as_of, observed, run_id, detail = attempts[0]
        detail = detail or {}
        ttl = int(step.get("ttl_days") or 30)
        if observed is not None and hasattr(observed, "tzinfo"):
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).days
            if age > ttl:        # an old lane record is history, not exhaustion: the lane must run again
                return AttemptResult("UNWIRED", error={"class": "LANE_STALE", "permanent": False,
                                                       "message": f"T6 lane attempt is {age}d old (> {ttl}d)"})
        documented = {"route": "T6", "source": source, "source_as_of": as_of, "lane_run_id": run_id,
                      "lane_observed_at": _iso(observed), "resolution": detail.get("resolution"),
                      "estate_row_ids": rows, "record": "public.free_route_attempts"}
        if outcome == "HIT":
            found = self._phone_evidence(rows, roles=None, source_family="NCSBE_PUBLIC_VOTER_FILE",
                                         source_effective_date=as_of, route="T6")
            if found:
                return AttemptResult("HIT", evidence_found=found)
            return AttemptResult("ERROR", error={"class": "SPINE_INCONSISTENT", "permanent": False,
                                                 "message": "T6 HIT has no projected NCSBE phone for the parcel"})
        if outcome in ("NO_HIT", "HALTED"):
            return AttemptResult("NO_HIT" if outcome == "NO_HIT" else "HALTED", evidence_not_found=documented)
        return AttemptResult("ERROR", error={"class": f"T6_{outcome}", "permanent": False, "message": str(detail)[:200]})

    def _graph(self, state: JobState, *, roles, route: str) -> AttemptResult:
        if state.fact != "contact.phone":
            return AttemptResult("NOT_APPLICABLE", evidence_not_found={"route": route,
                                                                        "reason": f"{route} yields phones only"})
        rows = self.estate_rows(state.county_fips, state.opened["target"].get("apn"))
        if not rows:
            return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                   "message": "parcel is not in the estate's identity graph"})
        found = self._phone_evidence(rows, roles=roles, source_family=None, source_effective_date=None, route=route)
        if found:
            return AttemptResult("HIT", evidence_found=found)
        parties = self._query(PARTY_COUNT_SQL, {"rows": rows, "roles": list(roles) if roles else None})[0][0]
        if roles and not parties:
            return AttemptResult("NOT_APPLICABLE", evidence_not_found={
                "route": route, "reason": "no heir / representative party is linked to this parcel",
                "estate_row_ids": rows})
        return AttemptResult("NO_HIT", evidence_not_found={
            "route": route, "parties_linked": int(parties), "estate_row_ids": rows, "searched_at": utc_now_iso(),
            "reason": "no linked party has a phone in the identity graph" if parties
            else "no party is linked to this parcel in the identity graph"})

    def i2(self, step: Mapping[str, Any], state: JobState) -> AttemptResult:
        return self._graph(state, roles=None, route="I2")

    def i3(self, step: Mapping[str, Any], state: JobState) -> AttemptResult:
        return self._graph(state, roles=HEIR_ROLES, route="I3")

    def executors(self) -> Dict[str, Executor]:
        return {"route.T6": self.t6, "route.I2": self.i2, "route.I3": self.i3}


def default_executors(engine_tenant: str = DEEDSONAR_TENANT, *, engine_conn: Any = None,
                      spine_conn: Any = None) -> Dict[str, Executor]:
    """Every free step this engine can run itself: county parcel tiers, its own cache, and the
    spine-backed contact routes. Steps without an executor here are UNWIRED in the runner.
    ``engine_tenant`` is the engine's data tenant (landos.owner_leads), not a caller's tenant."""
    parcel = parcel_tier_executor(engine_tenant, local_cache_conn=engine_conn)
    out: Dict[str, Executor] = {"county_rest": parcel, "fetchgis": parcel, "colligo": parcel, "local_cache": parcel}
    if spine_conn is not None:
        out.update(SpineRoutes(spine_conn).executors())
    return out
