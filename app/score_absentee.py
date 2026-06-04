"""
score_absentee.py — the absentee-owner scoring layer. PURE Python: no DB, no
network. Every function takes plain dicts/sets and returns plain values, so the
whole thing is unit-testable in isolation (the DB join lives in scripts/owner_leads.py,
which feeds this module already-loaded signals + overlay hits).

WHAT THIS PREDICTS (honesty rule, stated up front and repeated in output):
  Absentee ownership is NOT a guarantee of motivation. It is a proxy for two
  things: (1) REACHABILITY — an off-parcel mailing address is where outreach lands;
  and (2) LIKELIHOOD of disposition — an owner who doesn't live on the land is
  structurally more willing to sell it. We TIGHTEN that likelihood with signal
  overlap (active distress + opportunity-zone demand). The output is a PRIORITIZED
  list, not a list of guaranteed sellers.

THE FOUR PIECES
  1. owner_type      — individual | entity | govt   (from owner-name patterns)
  2. absentee_tier   — out_of_state > out_of_county > out_of_town > owner_occupied
  3. absentee_score  — tier weight, decayed by distance, adjusted by owner_type
  4. signal_overlap  — which OTHER landos signals this parcel also carries (+OZ)
  then final_lead_score = absentee_score fused (multiplicatively) with overlap.

THE FUSION IS THE EDGE: an absentee owner who is ALSO tax-delinquent or sits in an
opportunity zone must rank top-decile. The multiplier below makes that real, not
cosmetic — see final_lead_score().
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Set

# Scorer version — recorded into every run manifest / run_log row so any produced
# lead list is tied to the exact scoring logic that made it.
#   1.0.0 — initial absentee fusion (hard clamp to 100)
#   1.1.0 — de-compressed ranking: unclamped raw fusion (raw_fused_score) + batch
#           normalization to 0–100 + explicit tiebreakers. Does NOT change which
#           signals count or any weight — only how the scores spread + resolve ties.
__VERSION__ = "1.1.0"

# ---------------------------------------------------------------------------
# County membership reference (multi-county; keyed by FIPS — Berrien is first).
# Used ONLY to answer "is this mailing address in the SAME county as the situs?"
# so we can separate out_of_county from out_of_town. ZIP/place lists are real
# USPS/Census values for the county. A ZIP/place not listed but in-state simply
# reads as out_of_county — an honest, conservative default.
# ---------------------------------------------------------------------------
COUNTY_GEO: Dict[str, Dict[str, Any]] = {
    "26021": {  # Berrien County, MI
        "name": "Berrien",
        "state": "MI",
        "zips": {
            "49022", "49023", "49038", "49039", "49085", "49098", "49101", "49102",
            "49103", "49104", "49106", "49107", "49111", "49115", "49116", "49117",
            "49119", "49120", "49125", "49126", "49127", "49128", "49129", "49130",
        },
        "places": {
            "BENTON HARBOR", "BENTON", "ST JOSEPH", "SAINT JOSEPH", "COLOMA",
            "WATERVLIET", "BARODA", "BERRIEN CENTER", "BERRIEN SPRINGS", "BRIDGMAN",
            "BUCHANAN", "EAU CLAIRE", "HARBERT", "LAKESIDE", "NEW BUFFALO",
            "NEW TROY", "NILES", "SAWYER", "SODUS", "STEVENSVILLE", "THREE OAKS",
            "UNION PIER", "GALIEN", "SHOREHAM", "LINCOLN TOWNSHIP",
        },
    },
    "18141": {  # St. Joseph County, IN
        "name": "St. Joseph",
        "state": "IN",
        "zips": {
            # South Bend
            "46601", "46613", "46614", "46615", "46616", "46617", "46619", "46628",
            "46635", "46637",
            # Mishawaka
            "46544", "46545", "46546",
            # rest of county
            "46530",  # Granger
            "46536",  # Lakeville
            "46552",  # New Carlisle
            "46554",  # North Liberty
            "46556",  # Notre Dame
            "46561",  # Osceola
            "46574",  # Walkerton
        },
        "places": {
            "SOUTH BEND", "MISHAWAKA", "GRANGER", "WALKERTON", "NORTH LIBERTY",
            "NEW CARLISLE", "LAKEVILLE", "OSCEOLA", "NOTRE DAME", "ROSELAND",
            "INDIAN VILLAGE",
        },
    },
}

# ---- owner_type classification --------------------------------------------
# Government / public owners (a disposition CHANNEL — land bank, state surplus,
# tax-reverted — rather than a private "absentee owner").
_GOVT_PATTERNS = [
    "STATE OF", "COUNTY OF", "CITY OF", "VILLAGE OF", "TOWNSHIP OF", "TWP OF",
    " TOWNSHIP", " TWP", "LAND BANK", "LANDBANK", "TREASURER", "DEPARTMENT",
    "DEPT OF", " DNR", "NATURAL RESOURCES", "UNITED STATES", " USA", "U S A",
    "FEDERAL", "ROAD COMMISSION", "DRAIN COMMISSION", "SCHOOL DISTRICT",
    "PUBLIC SCHOOLS", "BOARD OF EDUCATION", "HOUSING COMMISSION",
    "HOUSING AUTHORITY", "REDEVELOPMENT AUTHORITY", "STATE LAND", "MICHIGAN STATE OF",
]
# Private non-person entities (an absentee one is a classic motivated-seller candidate).
_ENTITY_PATTERNS = [
    " LLC", "L.L.C", "L L C", " INC", " CORP", " CO.", " CO ", " COMPANY", " LTD",
    " LP", " LLP", " L P ", " TRUST", " TR ", " TR.", " ASSOCIATION", " ASSN",
    " COMMUNITY", " HOLDINGS", " PROPERTIES", " PROPERTY", " PARTNERS",
    " PARTNERSHIP", " VENTURES", " GROUP", " ENTERPRISES", " FOUNDATION",
    " CHURCH", " MINISTRIES", " BANK", " DEVELOPMENT", " BUILDERS", " HOMES",
    " REALTY", " INVESTMENTS", " CAPITAL", " FARMS", " ESTATES", " &",
]


def classify_owner_type(owner_name: Optional[str]) -> str:
    """Return 'govt' | 'entity' | 'individual' from the owner name. Govt is checked
    first (a 'CITY OF ... LLC' would be govt), then entity, else individual."""
    if not owner_name:
        return "individual"
    n = f" {owner_name.upper().strip()} "
    if any(p in n for p in _GOVT_PATTERNS):
        return "govt"
    if any(p in n for p in _ENTITY_PATTERNS):
        return "entity"
    return "individual"


# ---- absentee tier ---------------------------------------------------------
_DIR = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST"}
_SUFFIX = {
    "ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
    "CT", "COURT", "BLVD", "HWY", "HIGHWAY", "WAY", "PL", "PLACE", "TER", "TERRACE",
    "CIR", "CIRCLE", "PKWY", "PARKWAY", "PATH", "TRL", "TRAIL", "PO", "POB", "BOX",
}


def _norm_place(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z ]", "", (s or "").upper()).strip()


def _street_tokens(addr: Optional[str]):
    """(house_number_or_None, set_of_street_name_tokens) from a street line."""
    if not addr:
        return (None, set())
    toks = re.sub(r"[^A-Z0-9 ]", " ", addr.upper()).split()
    house = toks[0] if toks and toks[0].isdigit() else None
    name = {t for t in toks if not t.isdigit() and t not in _DIR and t not in _SUFFIX}
    return (house, name)


def _is_owner_occupied(situs: Dict[str, Any], mailing: Dict[str, Any]) -> bool:
    """True only when the mailing address is the SAME physical address as the situs.
    A vacant lot (situs house number '0' or missing) can never be owner-occupied."""
    s_house, s_name = _street_tokens(situs.get("address"))
    m_house, m_name = _street_tokens(mailing.get("address"))
    if not s_house or s_house == "0" or not m_house:
        return False
    if s_house != m_house:
        return False
    if not (s_name & m_name):  # need at least one shared street-name token
        return False
    # confirm same locality (zip preferred, else city)
    s_zip, m_zip = (situs.get("zip") or "")[:5], (mailing.get("zip") or "")[:5]
    if s_zip and m_zip:
        return s_zip == m_zip
    return _norm_place(situs.get("city")) == _norm_place(mailing.get("city"))


def _in_situs_county(mailing: Dict[str, Any], county_geo: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Is the mailing address inside the situs county? True/False, or None if we
    cannot tell (no county_geo reference and no city match)."""
    if not county_geo:
        return None
    m_zip = (mailing.get("zip") or "")[:5]
    if m_zip and m_zip in county_geo.get("zips", set()):
        return True
    m_city = _norm_place(mailing.get("city"))
    if m_city and m_city in county_geo.get("places", set()):
        return True
    if m_zip or m_city:
        return False  # we have a locality and it's not in the county
    return None


def classify_absentee_tier(
    situs: Dict[str, Any], mailing: Dict[str, Any],
    county_geo: Optional[Dict[str, Any]] = None,
) -> str:
    """out_of_state | out_of_county | out_of_town | owner_occupied | unknown_situs | unknown.

    SITUS-DEGRADE (some counties, e.g. St. Joseph IN, often lack a situs street address):
    out_of_state and out_of_county resolve from state/county alone — no situs street
    needed. The owner_occupied-vs-out_of_town distinction DOES need the situs street; when
    it's absent we return 'unknown_situs' rather than silently calling it owner-occupied
    (and never fabricate a situs)."""
    m_state = (mailing.get("state") or "").upper().strip()
    s_state = (situs.get("state") or "").upper().strip()
    # no mailing locality at all -> cannot classify (don't fabricate a tier)
    if not (mailing.get("address") or mailing.get("city") or m_state):
        return "unknown"
    # out_of_state resolves from state mismatch alone (no situs street required)
    if m_state and s_state and m_state != s_state:
        return "out_of_state"
    same_county = _in_situs_county(mailing, county_geo)
    # out_of_county resolves from county membership alone (no situs street required)
    if same_county is False:
        return "out_of_county"
    # same county (or county-undeterminable) -> need the situs STREET to separate
    # owner_occupied from out_of_town.
    s_house, _ = _street_tokens(situs.get("address"))
    situs_has_street = bool(s_house) and s_house != "0"
    if not situs_has_street:
        # situs street unknown -> cannot determine occupancy; do NOT assume occupied
        return "unknown_situs"
    if _is_owner_occupied(situs, mailing):
        return "owner_occupied"
    return "out_of_town"


# ---- absentee score --------------------------------------------------------
# Distance-decayed base: the farther the owner is from the land, the more
# reachability/disposition signal an absentee mailing carries.
TIER_BASE = {
    "out_of_state": 90.0,
    "out_of_county": 70.0,
    "out_of_town": 50.0,
    "owner_occupied": 20.0,
    "unknown_situs": 40.0,   # in/near county but situs street unknown -> occupancy undetermined
    "unknown": 40.0,
}
# owner_type adjustment: an ABSENTEE entity is a classic motivated-seller candidate.
OWNER_TYPE_FACTOR = {"entity": 1.10, "individual": 1.00, "govt": 1.00}
# Govt land is frequently disposable (land bank / state surplus / tax-reverted),
# regardless of the agency HQ mailing — so govt gets a disposition floor.
GOVT_DISPOSITION_FLOOR = 75.0


def absentee_score(tier: str, owner_type: str) -> float:
    base = TIER_BASE.get(tier, TIER_BASE["unknown"])
    score = base * OWNER_TYPE_FACTOR.get(owner_type, 1.0)
    if owner_type == "govt":
        score = max(score, GOVT_DISPOSITION_FLOOR)
    return round(max(0.0, min(100.0, score)), 2)


def absentee_flag(tier: str) -> bool:
    """True when the owner is NOT at the parcel (any off-parcel tier)."""
    return tier in ("out_of_state", "out_of_county", "out_of_town")


# ---- signal overlap (cross-signal join, already loaded by the caller) ------
DISTRESS_SIGNALS = {
    "tax_delinquent", "brownfield", "environmental_part201",
    "pre_foreclosure", "foreclosure", "code_violation", "probate", "vacancy",
}


def build_signal_overlap(
    signal_types: Iterable[str], oz_hit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize which existing landos signals this parcel ALSO carries, plus OZ."""
    types: Set[str] = {str(s) for s in (signal_types or []) if s}
    distress = sorted(types & DISTRESS_SIGNALS)
    return {
        "signals": sorted(types),
        "distress_stack": distress,
        "signal_count": len(types),
        "in_opportunity_zone": bool(oz_hit),
        "oz_tract": (oz_hit or {}).get("name"),
        "oz_provenance": (oz_hit or {}).get("provenance_status"),
    }


# ---- final lead score: absentee × signal-overlap fusion --------------------
# overlap_multiplier starts at 1.0 (no overlap = absentee score stands alone) and
# is lifted by distress + opportunity-zone overlap, capped. This is what pushes an
# absentee owner who is ALSO distressed/OZ into the top decile.
TAX_DELINQUENT_BUMP = 0.20     # active foreclosure pipeline = strongest motivation
OTHER_DISTRESS_BUMP = 0.10     # per additional distress signal (brownfield, env)
OTHER_DISTRESS_CAP = 2         # at most 2 counted (0.20 max from "other")
OZ_BUMP = 0.15                 # opportunity-zone = exit/buyer-demand premium
OVERLAP_CAP = 1.60             # ceiling on the multiplier


def overlap_multiplier(signal_overlap: Dict[str, Any]) -> float:
    distress = set(signal_overlap.get("distress_stack") or [])
    mult = 1.0
    if "tax_delinquent" in distress:
        mult += TAX_DELINQUENT_BUMP
    other = distress - {"tax_delinquent"}
    mult += OTHER_DISTRESS_BUMP * min(len(other), OTHER_DISTRESS_CAP)
    if signal_overlap.get("in_opportunity_zone"):
        mult += OZ_BUMP
    return round(min(mult, OVERLAP_CAP), 4)


def final_lead_score(abs_score: float, signal_overlap: Dict[str, Any]) -> float:
    """final_lead_score = clamp(absentee_score * overlap_multiplier, 0, 100).

    Example: an out-of-state LLC (90 * 1.10 = 99) that is tax-delinquent AND in an
    OZ -> multiplier 1 + 0.20 + 0.15 = 1.35 -> 99 * 1.35 = 133 -> clamped to 100.
    A lone absentee with no overlap keeps its absentee score unchanged."""
    return round(max(0.0, min(100.0, abs_score * overlap_multiplier(signal_overlap))), 2)


def raw_fused_score(abs_score: float, signal_overlap: Dict[str, Any]) -> float:
    """UNCLAMPED fusion = absentee_score * overlap_multiplier (v1.1.0).

    The hard 100 clamp in final_lead_score() was piling many 99-base out-of-state
    entities at the ceiling, hiding the difference between ONE stacked signal and
    THREE. This raw score lets the fusion separate them (e.g. 99*1.55=153.45 vs
    99*1.10=108.9). The script normalizes the batch's raw scores back to 0–100 for
    display, so the ranking spreads across the range instead of piling at 100.
    Values and weights are identical to v1.0.0 — only the clamp is lifted."""
    return round(abs_score * overlap_multiplier(signal_overlap), 4)


# Tier ordering for tiebreaks (higher = farther from the parcel = stronger signal).
TIER_RANK = {"out_of_state": 4, "out_of_county": 3, "out_of_town": 2,
             "owner_occupied": 1, "unknown_situs": 0, "unknown": 0}


def tiebreak_key(raw_score: float, signal_overlap: Dict[str, Any],
                 absentee_tier: str, acreage: Any, owner_type: str) -> tuple:
    """Total-order sort key (sort DESCENDING). Primary is the de-compressed raw
    score; when scores are close/equal it falls through to the explicit tiebreakers,
    applied IN ORDER:
      (1) number of distinct distress signals
      (2) tax_delinquent present (active foreclosure beats passive environmental)
      (3) out_of_state > out_of_county > out_of_town
      (4) acreage (bigger lot = bigger deal)
      (5) individual owner over entity (cold-outreach responsiveness)"""
    distress = signal_overlap.get("distress_stack") or []
    try:
        acres = float(acreage)
    except (TypeError, ValueError):
        acres = 0.0
    return (
        round(float(raw_score or 0.0), 4),          # primary: the score itself
        len(distress),                               # (1)
        1 if "tax_delinquent" in distress else 0,    # (2)
        TIER_RANK.get(absentee_tier, 0),             # (3)
        acres,                                       # (4)
        1 if owner_type == "individual" else 0,      # (5)
    )


def weights_snapshot() -> Dict[str, Any]:
    """The exact fusion constants in effect, copied into each run manifest so a
    produced lead list is reproducible. Reads the live module constants — if any
    weight changes, the snapshot (and thus the manifest) reflects it automatically."""
    return {
        "scorer_version": __VERSION__,
        "tier_base": dict(TIER_BASE),
        "owner_type_factor": dict(OWNER_TYPE_FACTOR),
        "govt_disposition_floor": GOVT_DISPOSITION_FLOOR,
        "tax_delinquent_bump": TAX_DELINQUENT_BUMP,
        "other_distress_bump": OTHER_DISTRESS_BUMP,
        "other_distress_cap": OTHER_DISTRESS_CAP,
        "oz_bump": OZ_BUMP,
        "overlap_cap": OVERLAP_CAP,
    }


# ---- convenience: score one lead end-to-end (still pure) -------------------
def score_lead(
    lead: Dict[str, Any],
    signal_types: Iterable[str],
    oz_hit: Optional[Dict[str, Any]] = None,
    county_geo: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute every absentee field for one owner lead.

    `lead` carries situs + mailing fields (the script pulls situs locality out of
    raw_meta). Returns the columns scripts/owner_leads.py writes back."""
    situs = {
        "address": lead.get("situs_address"),
        "city": lead.get("situs_city"),
        "county": lead.get("situs_county"),
        "state": lead.get("situs_state"),
        "zip": lead.get("situs_zip"),
    }
    mailing = {
        "address": lead.get("owner_mailing_address"),
        "city": lead.get("owner_mailing_city"),
        "state": lead.get("owner_mailing_state"),
        "zip": lead.get("owner_mailing_zip"),
    }
    owner_type = classify_owner_type(lead.get("owner_name"))
    tier = classify_absentee_tier(situs, mailing, county_geo)
    a_score = absentee_score(tier, owner_type)
    overlap = build_signal_overlap(signal_types, oz_hit)
    f_score = final_lead_score(a_score, overlap)
    return {
        "owner_type": owner_type,
        "absentee_tier": tier,
        "absentee_flag": absentee_flag(tier),
        "absentee_score": a_score,
        "signal_overlap": overlap,
        "final_lead_score": f_score,            # clamped 0–100 (per-lead, legacy)
        "raw_fused_score": raw_fused_score(a_score, overlap),  # unclamped (v1.1.0)
    }
