"""
models.py — the request shapes for each endpoint.

FastAPI uses these Pydantic classes to validate incoming JSON automatically.
If APG sends a malformed request, FastAPI rejects it with a clear 422 error
BEFORE any of our code runs. Defaults mean every field is optional — a parcel
with only a couple of known signals still scores fine.
"""
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


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


# --- HUNT-V2: net-new territory hunt jobs (system proposes, operator fires) ---
class HuntTerritoryEntry(BaseModel):
    """One county. fips wins when given; else (state, county) must resolve
    against the census table (app/fips.py) — never guessed."""
    fips: Optional[str] = None
    state: Optional[str] = None
    county: Optional[str] = None


class HuntRequest(BaseModel):
    territory: list[HuntTerritoryEntry]
    recipe: Optional[dict] = None       # {signal_types: [...], thresholds: {...}}
    requested_by: Optional[str] = None


# --- FREE-FIRST: loopback research (one fact about one target per item) ---
class FreeFirstResearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str
    target: dict                       # {target_key, apn?, owner_name?, ...}
    state: str
    county_fips: str
    extra_free_sources: list[dict] = []
    supplied_evidence: dict = {}       # answers the caller's OWN extra steps only


class FreeFirstResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1)
    requested_by: dict
    parent_receipt: Optional[dict] = None
    items: list[FreeFirstResearchItem] = Field(min_length=1, max_length=200)
    probe_paid: bool = True


# --- RECIPE-2: composer (compose a goal into a proposal; decide it) ---
class DispoInventoryItem(BaseModel):
    lead_id: str
    disposition_id: Optional[str] = None
    county_fips: Optional[str] = None


class DispoPayload(BaseModel):
    inventory: list[DispoInventoryItem] = []
    params: dict = {}


class ComposeRequest(BaseModel):
    goal_type: str                       # 'acquire' | 'dispose'
    goal_text: str
    dispo: Optional[DispoPayload] = None


class DecideRequest(BaseModel):
    action: str                          # 'approve' | 'reject'
    note: Optional[str] = None
    decided_by: Optional[str] = None     # profiles.id (uuid), stamped by the CRM


class ComplianceCertBatchRequest(BaseModel):
    # A data buyer / tenant pulls a batch of Compliance Certificates for leads they hold.
    lead_ids: List[str]                  # engine owner_leads.lead_id OR board acquisition_leads.lead_id
    format: Optional[str] = "json"       # "json" (programmatic) | "html" (rendered document)


class FieldPinCaptureRequest(BaseModel):
    """FIELD PIN V1 capture (windshield sensor). The GPS observation as the phone
    saw it; the engine builds the ray, resolves it, and receipts the capture.
    Deep validation (ranges, vocabularies, timestamps) is fail-closed in
    app/field_pins.validate_capture — this model only carries the shape."""
    tenant_id: str
    device_id: str
    lat: float
    lon: float
    observed_at: str                     # ISO-8601 WITH timezone; the sighting moment
    heading_source: str                  # gps_course | fix_delta | compass | unknown
    side: str                            # left | right | ahead | at | unknown
    category: str
    accuracy_m: Optional[float] = None
    heading_deg: Optional[float] = None  # required unless heading_source='unknown'
    speed_mps: Optional[float] = None
    assumed_offset_m: Optional[float] = None
    spoken_text: Optional[str] = None
    operator_label: Optional[str] = None
    idempotency_key: Optional[str] = None  # replay-safe capture; same key -> same pin


class OpportunityIntelligenceRequest(BaseModel):
    """Portable Recipe Engine -> LandOS intelligence request.

    Deep validation is intentionally owned by ``app.opportunity_intelligence``
    because it validates the recipe's own versioned vocabulary as well as the
    request.  Extra top-level fields are preserved for forward-compatible
    fixture notices/extensions rather than silently dropped by Pydantic.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str
    as_of: str
    recipe: dict
    subject: dict
    asset: dict
    signals: list[dict]
    property_population: dict
    readiness: dict
    research_attempts: list[dict]
    lineage: dict
    resolution_context: Optional[dict] = None
    extensions: Optional[dict] = None
