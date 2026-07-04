"""
models.py — the request shapes for each endpoint.

FastAPI uses these Pydantic classes to validate incoming JSON automatically.
If APG sends a malformed request, FastAPI rejects it with a clear 422 error
BEFORE any of our code runs. Defaults mean every field is optional — a parcel
with only a couple of known signals still scores fine.
"""
from typing import Optional
from pydantic import BaseModel


class DistressSignals(BaseModel):
    tax_delinquent: bool = False
    probate: bool = False
    pre_foreclosure: bool = False
    code_violation: bool = False
    vacancy: bool = False
    long_hold: bool = False


class OwnerProfile(BaseModel):
    is_entity: bool = False
    age_band: str = "other"  # "senior" | "boomer" | "other"
    out_of_state: bool = False
    no_succession: bool = False


class ArbitrageInputs(BaseModel):
    list_price_per_acre: float = 0
    market_price_per_acre: float = 0
    acreage: float = 0
    subdivisions_to_2x: int = 10
    carry_feasible_90d: bool = False


class EvaluateRequest(BaseModel):
    tenant_id: str
    parcel_apn: str
    owner_hash: str
    county: Optional[str] = None
    state: Optional[str] = None
    profile: str = "default"  # which scoring profile's active weights to use
    distress: DistressSignals = DistressSignals()
    owner: OwnerProfile = OwnerProfile()
    arbitrage: ArbitrageInputs = ArbitrageInputs()


class LandDDRequest(BaseModel):
    """NVE-LAND due-diligence input. Tolerant of the CRM proxy body
    ({lead_id, parcel_apn, county_fips, lat, lng}) and the terse {apn, county_fips}.
    All optional: a lead with only coords (no APN) resolves via a county point-lookup."""
    county_fips: Optional[str] = None
    parcel_apn: Optional[str] = None
    apn: Optional[str] = None            # alias for parcel_apn
    lead_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    tenant_id: Optional[str] = None
    samples: int = 6                     # interior sample points per geo feed (flood/wetland/slope)


class ClaimRequest(BaseModel):
    tenant_id: str
    node_id: str
    claimed_by: str


class ReleaseRequest(BaseModel):
    tenant_id: str
    node_id: str


class FeedbackRequest(BaseModel):
    tenant_id: str
    node_id: str
    outcome: str  # "closed" | "dead" | "passed"
    projected_multiple: Optional[float] = None
    actual_multiple: Optional[float] = None
    notes: Optional[str] = None


# --- Emerging / Heat Map models (for pre-market boomer opportunities) ---
class EmergingHeatMapRequest(BaseModel):
    tenant_id: str
    county_fips: Optional[str] = None
    min_emerging_score: float = 50.0
    include_dale_local: bool = True
    limit: int = 500


class EmergingOpportunity(BaseModel):
    lead_id: str
    parcel_apn: Optional[str] = None
    owner_name: Optional[str] = None
    situs_address: Optional[str] = None
    county_fips: Optional[str] = None
    base_score: Optional[float] = None
    emerging_score: float
    emerging_signals: dict = {}
    dale_local: bool = False
    lat: Optional[float] = None
    lng: Optional[float] = None
    last_signal_date: Optional[str] = None


class EmergingHeatMapResponse(BaseModel):
    tenant_id: str
    count: int
    opportunities: list[EmergingOpportunity]
    note: str = "Emerging signals from LandOS discovery (deltas, negative space, boomer life events, DALE local). Use for heat map layers in CRM. Scoring/fusion in LandOS."
