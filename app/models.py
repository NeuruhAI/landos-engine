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
