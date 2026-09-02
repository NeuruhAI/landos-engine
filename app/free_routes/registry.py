"""FREE_ROUTES_REGISTER as versioned data (docs/FREE_ROUTES_REGISTER.md is rendered
from this). Built on app/resolution/sources.py (nc_source_registry.v1); this is
the route layer: which adapter runs, in what yield order, under which terms."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List

FREE_ROUTES_REGISTER_VERSION = "free_routes_register.v1"

AUTOMATED, MANUAL, BLOCKED = "AUTOMATED", "MANUAL", "BLOCKED"


@dataclass(frozen=True)
class Route:
    route_id: str
    name: str
    family: str            # traditional_heirs | traditional_owners | invented
    yield_order: int
    mode: str              # AUTOMATED | MANUAL | BLOCKED
    source: str
    provenance_url: str
    license: str           # quoted terms / license statement
    yields: str            # what the route can add
    writer: str            # the single writer path the adapter uses
    adapter: str           # module.function
    halt: str = ""         # H-11 | H-12 when mode != AUTOMATED
    human_step: str = ""

    def to_row(self) -> dict:
        return asdict(self)


ROUTES: List[Route] = [
    Route("T1", "NC eCourts estate files (AOC-E-201/505 PDFs)", "traditional_heirs", 1, MANUAL,
          "NC Judicial Branch eCourts Portal (Odyssey) Smart Search", "https://portal-nc.tylertech.cloud/Portal/",
          "Public case information reachable in an ordinary browser; scripted clients receive HTTP 403 and Smart Search sits behind a human-verification challenge (never defeated).",
          "personal representative, heirs/devisees with mailing addresses, case number",
          "app.contact.estate_pdf_intake → importer CSV → project_contacts_to_deedsonar (parties / party_phones / contact_evidence / candidate_parties)",
          "app.free_routes.t1_ecourts_pdf.run", "H-12",
          "Download each decedent's AOC-E-201/AOC-E-505 PDF from the Portal into ~/.neuruh/private/deedsonar/_raw/estate/pdf/<county>/ (worklist: ESTATE_WORKLIST_<COUNTY>_<date>.csv, top 40 by rank)."),
    Route("T2", "Notice to Creditors (NC GS 28A-14-1) via ncnotices.com", "traditional_heirs", 2, MANUAL,
          "NC Press Association public notices (ncnotices.com)", "https://www.ncnotices.com/Search.aspx",
          "robots.txt disallows AI crawlers (anthropic-ai, ClaudeBot, GPTBot, Scrapy …); Terms of Use §3: no screen scraping, database scraping, spidering, or collection of personally identifiable information; §9 liquidated damages.",
          "personal representative name and mailing address, publication date, newspaper",
          "operator paste-back → estate importer CSV (role PR) → project_contacts_to_deedsonar",
          "app.free_routes.human_routes.t2_notice_search", "H-12",
          "Open Search.aspx, keywords = decedent name (Exact Phrase), County = the county, date range = last 24 months; copy the PR's name and mailing address and the publication date into the importer CSV."),
    Route("T3", "Special proceedings (partition / foreclosure) on eCourts", "traditional_heirs", 3, MANUAL,
          "NC eCourts Portal Smart Search (case type Special Proceeding)", "https://portal-nc.tylertech.cloud/Portal/",
          "As T1.", "every heir named as a party, with addresses in the petition",
          "same PDF path as T1", "app.free_routes.human_routes.t3_special_proceedings_search", "H-12",
          "Smart Search by decedent / owner name, filter Special Proceedings; download the petition PDF into the same drop folder."),
    Route("T4", "Obituaries (legacy.com, funeral homes, newspapers)", "traditional_heirs", 4, MANUAL,
          "legacy.com and newspaper obituary pages", "https://www.legacy.com/obituaries/search",
          "legacy.com Terms of Use prohibit robots, spiders, crawlers or extraction software; IP blocking on request frequency.",
          "heir candidate names, cities, relationships from the 'survived by' paragraph (confidence=obituary, never a resolved address)",
          "app.free_routes.obituary.parse_survived_by → heir_candidate rows → contact_evidence (role heir_candidate)",
          "app.free_routes.human_routes.t4_obituary_search", "H-12",
          "Open the search URL for the decedent, paste the 'survived by' paragraph into the Routes panel; the parser stores heir candidates."),
    Route("T5", "NC Secretary of State entities (LLC / corp / trust owners)", "traditional_owners", 5, MANUAL,
          "NC SOS Business Registration search; per-county Non-Profit reports; IRS EO BMF", "https://www.sosnc.gov/online_services/search/by_title/_Business_Registration",
          "SOS: automated or scripted searching is not permitted; bulk Business Registration data is a paid Data Subscription (contract; price set by contract) — BLOCKED at ALLOW_SPEND=0 (H-11); IRS EO BMF is a free public bulk file.",
          "registered agent, officers, principal office, mailing address, SOS ID",
          "app.contact.entity_public (SOS_IMPORT_COLUMNS) → project_contacts_to_deedsonar",
          "app.free_routes.human_routes.t5_sos_search", "H-12",
          "Look the entity up by exact name, record SOSID / status / registered agent / officials into sos_import_template.csv (registered agent never labelled owner)."),
    Route("T6", "NCSBE voter registration (all seven CURRENT counties)", "traditional_owners", 6, AUTOMATED,
          "NC State Board of Elections voter files (ncvoterNN.zip, weekly)", "https://s3.amazonaws.com/dl.ncsbe.gov/data/",
          "Public bulk files, free download, no access control; birthdate / SSN / driver's license excluded by state law.",
          "phone where present, mailing address, registration date, age band; matched on name + address",
          "scripts/run_nc_free_phone_enrichment.py (delta refresh) → project_contacts_to_deedsonar --apply",
          "app.free_routes.t6_ncsbe.run"),
    Route("T7", "County Register of Deeds (Wake / Mecklenburg / Durham)", "traditional_owners", 7, MANUAL,
          "Wake ROD Books, Mecklenburg ROD (Manatron), Durham ROD", "https://services.wake.gov/booksweb/",
          "Interactive public search; Wake and Mecklenburg sit behind human-verification walls for scripted clients (never defeated). Wake's daily tax file already carries deed book / page / date for every delinquent account (see T8).",
          "grantor / grantee names, instrument mailing addresses, mortgage holder, liens, years_owned",
          "operator paste-back → deed import CSV → lead_import_rows deed fields (writer: engine importer)",
          "app.free_routes.human_routes.t7_rod_search", "H-12",
          "Search the ROD by owner name for the PULL_DEED_MORTGAGE_LIENS queue (306) then tier A/B; record book/page, instrument date, grantor, mailing address on the instrument."),
    Route("T8", "County open data (delinquent tax, code enforcement, vacancy)", "traditional_owners", 8, AUTOMATED,
          "Wake Tax Administration daily delinquent file; Charlotte HNS code-enforcement ArcGIS services; Mecklenburg delinquent publication; Durham / Raleigh portals", "https://services.wake.gov/collection_extracts/",
          "Wake: free public extract refreshed daily (no terms shown); Charlotte ArcGIS REST: public, no copyright text, maxRecordCount 750; Mecklenburg publication: PDF behind a viewer page; Durham / Raleigh: dataset to be named by the operator.",
          "signals (delinquency amount and years, code cases) and mailing-address refresh with the file date as source_as_of — never phones",
          "landos.signals / field_observation_evidence via the engine signal loader; mailing refresh via lead_import_rows overlay",
          "app.free_routes.t8_open_data.run"),
    Route("T9", "USPS Addresses API v3 (standardize / validate)", "traditional_owners", 9, AUTOMATED,
          "USPS Developer Portal, Addresses 3.0 (default product)", "https://developers.usps.com/addressesv3",
          "Free with the default product; registration needs a USPS Business Account (cop.usps.com), an App, and OAuth client credentials; quota increase by request. NCOA is paid — never used.",
          "standardized mailing address, deliverability flag (DPV), validated_at as the address's source_as_of",
          "lead_import_rows.normalized mailing fields overlay (engine importer) with source=USPS_ADDRESS_V3",
          "app.free_routes.usps.run", "H-12",
          "Register at cop.usps.com, create an App, put USPS_CONSUMER_KEY / USPS_CONSUMER_SECRET into the engine env; the adapter then runs unattended."),
    Route("T10", "People search (TruePeopleSearch / FastPeopleSearch / Google), manual only", "traditional_owners", 10, MANUAL,
          "third-party people-search sites", "https://www.truepeoplesearch.com/",
          "Sites' terms forbid scraping and automated queries; DeedSonar never automates them — the operator clicks a prepared URL and records what they read.",
          "phones / relatives as operator-recorded, confidence=operator_web",
          "operator paste-back → contact_evidence via the Routes panel",
          "app.free_routes.human_routes.t10_people_search", "H-12",
          "Click the one-click URL in the Routes panel, read the page, paste back what applies."),
    Route("I1", "Out-of-state router (owner mailing state ≠ NC)", "invented", 11, AUTOMATED,
          "the estate itself (export_opportunities.py out_of_state flag)", "scripts/export_opportunities.py",
          "Internal data.", "a route of its own per owner: their home-state county records (ROD / assessor) as one-click URLs",
          "route receipts only (no contact writes)", "app.free_routes.invented.i1_out_of_state"),
    Route("I2", "Identity graph collapse (same person across parcels)", "invented", 12, AUTOMATED,
          "public.parties / candidate_parties / party_phones (landos identity model)", "migration 104 party model",
          "Internal data.", "one phone found anywhere attaches to every parcel the same party owns",
          "candidate_parties (writer: project_contacts_to_deedsonar idempotent keys)", "app.free_routes.invented.i2_identity_graph"),
    Route("I3", "Heir-name cross-match (heir × voter / SOS / ROD)", "invented", 13, AUTOMATED,
          "candidate_parties with role heir/family/authority × T5/T6/T7 sources", "internal",
          "As T5/T6/T7.", "contacts for the heir instead of the decedent on the 105 tier-A rows already linked to an heir party",
          "T6 lane on heir identities; T5/T7 URL lists per heir", "app.free_routes.invented.i3_heir_cross_match"),
    Route("I4", "Recipe receipts as routing (WHY components → route order)", "invented", 14, AUTOMATED,
          "lead_import_rows.recipe_engine receipt bag", "internal",
          "Internal data.", "the free-routes queue order per row: probate → T1/T2, entity → T5, absentee → I1/T7",
          "operator_queue / next_source (writer: refresh_next_best_source.py)", "app.free_routes.routing_order.route_order_for"),
    Route("I5", "Watches / deltas re-fire after every route run", "invented", 15, AUTOMATED,
          "DeedSonar watches delta engine", "/api/cron/watches",
          "Internal.", "the operator sees what changed after each route", "watch_deltas (existing writer)", "app.free_routes.invented.i5_refire_watches"),
]

FREE_ROUTES_EXHAUSTED_REQUIRES = tuple(r.route_id for r in ROUTES if r.route_id.startswith("T"))


def routes_by_mode(mode: str) -> List[Route]:
    return [r for r in ROUTES if r.mode == mode]


def render_markdown() -> str:
    lines = ["# FREE_ROUTES_REGISTER — every $0 contact/heir route, with its terms and its adapter",
             "", f"Version `{FREE_ROUTES_REGISTER_VERSION}` (rendered from `app/free_routes/registry.py`; built on `app/resolution/sources.py` nc_source_registry.v1).",
             "", "Founder direction, verbatim: *\"The option to scrub can be in there. We want to exhaust the ability to do stuff for free before we pay. Lots of free resources first. Search probate and all the traditional ways to do this plus the stuff we invented. We can quantify our list and know what info is up to date or not.\"*",
             "", "Rules every adapter obeys: reads a source with a stated license/ToS; writes only through the named writer path; stamps `source`, `source_as_of`, `observed_at`, `confidence`, `provenance_url`; never overwrites a higher-confidence value with a lower one; produces a receipt with a hit rate (`app/free_routes/receipts.py`). A route whose terms forbid scripts is MANUAL: it emits the operator's one-click URL list and an H-12 receipt — never a silent skip. `operator_queue = FREE_ROUTES_EXHAUSTED` is set only when every non-halted T-route has a receipt for the row (FR-2).",
             "", "| # | route | family | mode | source | terms / license | yields | writer path | adapter | halt | human step |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(ROUTES, key=lambda x: x.yield_order):
        lines.append(f"| {r.route_id} | {r.name} | {r.family} | {r.mode} | [{r.source}]({r.provenance_url}) | {r.license} | {r.yields} | `{r.writer}` | `{r.adapter}` | {r.halt or '—'} | {r.human_step or '—'} |")
    return "\n".join(lines) + "\n"
