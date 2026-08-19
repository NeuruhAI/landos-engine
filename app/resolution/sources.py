"""The source registry: what a $0 operator can lawfully obtain, per county.

T20D proved the automated free channel is finished and named the two unfed
operator routes. It did not say, per county, WHICH official office holds the
record, in what form, and under what terms. Without that a "manual route" is a
wish. This module is that answer, as versioned data so it can be tested and
diffed rather than living in a document.

ACQUISITION LAW FOR EVERY ROW HERE
  - No CAPTCHA is solved.
  - No authentication is bypassed.
  - No anti-automation token is replayed.
  - No rate control is evaded.
  - No source whose terms forbid scripted access is scripted.
A source that fails any of those is recorded as MANUAL with the exact operator
work package, not quietly automated.

DISCOVERY LOG (2026-08-19, this lane, live checks)
  www.nccourts.gov            -> HTTP 403 to non-browser clients, every path
                                 tried, including via an independent fetcher.
                                 The statewide judicial site is not automatable
                                 at all. Recorded as fact, not as "untried".
  portal-nc.tylertech.cloud   -> HTTP 403. The eCourts Portal is the statewide
                                 estate index and it is access-controlled.
  www.sosnc.gov               -> HTTP 200 at root. Business Registration search
                                 path 403s, and the SOS states scripted or
                                 automated searching is not permitted.
  SOS bulk business data      -> paid Data Subscription only. Out of scope at
                                 PROVIDER_SPEND_USD = 0.00.
  SOS per-county free reports -> "Non-Profits in each county" publishes a Text
                                 and an Excel file per county, no login, no
                                 payment, no CAPTCHA, refreshed nightly
                                 (page reported Last Run 8/19/2026 12:24 AM).
                                 The download itself is a POST carrying an
                                 anti-forgery token, so it is an INTERACTIVE
                                 control: one operator click per county, not a
                                 script. Free, lawful, and cheap.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

SOURCE_REGISTRY_VERSION = "nc_source_registry.v1"

# The seven CURRENT NC counties, with the FIPS the estate carries.
NC_COUNTIES: Dict[str, str] = {
    "Cabarrus": "37025",
    "Durham": "37063",
    "Gaston": "37071",
    "Johnston": "37101",
    "Mecklenburg": "37119",
    "Union": "37179",
    "Wake": "37183",
}

SOURCE_TYPES = (
    "OFFICIAL_BULK_DOWNLOAD",
    "OFFICIAL_STATIC_FILE",
    "OFFICIAL_INTERACTIVE_INDEX",
    "OFFICIAL_COUNTER_OR_WRITTEN_REQUEST",
    "OFFICIAL_EMAIL_REQUEST",
    "PAID_SUBSCRIPTION",
)

ACQUISITION = ("AUTOMATABLE", "MANUAL", "BLOCKED")


@dataclass(frozen=True)
class SourceEntry:
    county: str                 # "" means statewide
    source_family: str
    official_source: str
    source_type: str
    locator: str
    acquisition: str
    fmt: str
    fields: str
    coverage: str
    cost_usd: str
    terms: str
    last_checked: str
    blocking_reason: str = ""
    operator_steps: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        d["automatable"] = "YES" if self.acquisition == "AUTOMATABLE" else "NO"
        d["manual"] = "YES" if self.acquisition == "MANUAL" else "NO"
        return d


CHECKED = "2026-08-19"

# ---------------------------------------------------------------------------
# Estate / probate
# ---------------------------------------------------------------------------

_ECOURTS = SourceEntry(
    county="",
    source_family="PROBATE_INDEX",
    official_source="NC Judicial Branch eCourts Portal (Odyssey)",
    source_type="OFFICIAL_INTERACTIVE_INDEX",
    locator="https://portal-nc.tylertech.cloud/Portal/",
    acquisition="BLOCKED",
    fmt="HTML (interactive)",
    fields="case number; case type; party names; filing dates; documents where public",
    coverage="statewide; estate cases filed on/after the county's eCourts go-live",
    cost_usd="0.00",
    terms="Public case information. Access is controlled; the site refuses "
          "non-browser clients (HTTP 403 observed this lane).",
    last_checked=CHECKED,
    blocking_reason="Access control returns 403 to programmatic clients. "
                    "Scripting it would mean defeating that control, which this "
                    "lane forbids. Operator-in-browser use is unaffected.",
    operator_steps="Operator opens Portal in a browser, searches the decedent by "
                   "name and county, and transcribes the estate case number, "
                   "applicant / personal representative, and any named heir into "
                   "the estate import template.",
)

_ESTATE_COUNTER = {
    "Mecklenburg": (
        "Mecklenburg County Clerk of Superior Court - Estates Division",
        "OFFICIAL_EMAIL_REQUEST",
        "Mecklenburg.Estates@nccourts.org",
        "Estate files dated before 2023-10-09 are requested from the Estates "
        "Division by email; later filings appear in Portal.",
    ),
}

_ESTATE_DEFAULT = (
    "{county} County Clerk of Superior Court - Estates Division",
    "OFFICIAL_COUNTER_OR_WRITTEN_REQUEST",
    "{county} County Courthouse, Clerk of Superior Court, Estates Division",
    "Estate files are public records. The clerk's office provides public "
    "self-service terminals and fulfils written/counter requests for copies.",
)


def estate_sources() -> List[SourceEntry]:
    out: List[SourceEntry] = [_ECOURTS]
    for county in sorted(NC_COUNTIES):
        if county in _ESTATE_COUNTER:
            name, stype, locator, terms = _ESTATE_COUNTER[county]
        else:
            name, stype, locator, terms = _ESTATE_DEFAULT
            name = name.format(county=county)
            locator = locator.format(county=county)
        out.append(SourceEntry(
            county=county,
            source_family="ESTATE_IMPORT",
            official_source=name,
            source_type=stype,
            locator=locator,
            acquisition="MANUAL",
            fmt="paper / PDF / operator transcription",
            fields="estate case number; decedent; applicant; personal representative; "
                   "executor; administrator; named heirs; mailing addresses; attorney "
                   "(recorded for provenance, never contacted)",
            coverage="all estates administered in the county",
            cost_usd="0.00 for inspection; per-page copy fees may apply",
            terms=terms,
            last_checked=CHECKED,
            blocking_reason="",
            operator_steps=(
                "Take the county slice of ESTATE_IMPORT_WORKLIST.csv, inspect each "
                "decedent's estate file, and fill one row per LIVING person "
                "(applicant / PR / executor / administrator / named heir) in "
                "estate_import_template.csv. Never a row for the decedent."
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Entity / SOS
# ---------------------------------------------------------------------------

def entity_sources() -> List[SourceEntry]:
    out: List[SourceEntry] = [
        SourceEntry(
            county="",
            source_family="SOS_IMPORT",
            official_source="NC Secretary of State - Business Registration Search",
            source_type="OFFICIAL_INTERACTIVE_INDEX",
            locator="https://www.sosnc.gov/online_services/search/by_title/_Business_Registration",
            acquisition="MANUAL",
            fmt="HTML (interactive)",
            fields="legal name; SOSID; status; formation date; registered agent; "
                   "principal office; mailing office; company officials",
            coverage="all NC-registered entities",
            cost_usd="0.00",
            terms="Designed for interactive real-time use. The SOS states automated "
                  "or scripted searching is NOT permitted. Path 403s to script clients.",
            last_checked=CHECKED,
            blocking_reason="",
            operator_steps="Operator looks up the entity by name, records SOSID, "
                           "status, registered agent and company officials into "
                           "sos_import_template.csv. Registered agent is recorded "
                           "with its own role and is never labelled the owner.",
        ),
        SourceEntry(
            county="",
            source_family="SOS_IMPORT",
            official_source="NC Secretary of State - Business Registration Data Subscription",
            source_type="PAID_SUBSCRIPTION",
            locator="https://www.sosnc.gov/online_services/data_subscriptions",
            acquisition="BLOCKED",
            fmt="CSV, refreshed weekly",
            fields="name; home state; contact and address; foreign/domestic; "
                   "officers/partners/directors; status; filings; stock",
            coverage="all NC-registered entities, bulk",
            cost_usd="PAID - amount set by contract",
            terms="Subscription contract required.",
            last_checked=CHECKED,
            blocking_reason="Paid. PROVIDER_SPEND_USD is 0.00 in this lane, so the "
                            "only bulk entity route that exists is out of scope. This "
                            "is the honest boundary of the free entity channel.",
            operator_steps="",
        ),
        SourceEntry(
            county="",
            source_family="IRS_EO_ICO_NCSBE",
            official_source="IRS Exempt Organizations Business Master File (NC extract)",
            source_type="OFFICIAL_BULK_DOWNLOAD",
            locator="https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf",
            acquisition="AUTOMATABLE",
            fmt="CSV",
            fields="EIN; organization name; in-care-of name; street; city; state; ZIP; "
                   "ruling date; NTEE",
            coverage="NC exempt organizations",
            cost_usd="0.00",
            terms="Public bulk file, free download, no access control.",
            last_checked=CHECKED,
            blocking_reason="",
            operator_steps="",
        ),
    ]
    # The one genuinely free, genuinely per-county entity file that exists.
    for county in sorted(NC_COUNTIES):
        out.append(SourceEntry(
            county=county,
            source_family="SOS_IMPORT",
            official_source="NC Secretary of State - Non-Profits in each county",
            source_type="OFFICIAL_STATIC_FILE",
            locator="https://www.sosnc.gov/online_services/business_registration/Non_Profit_Reports",
            acquisition="MANUAL",
            fmt="Excel and plain text, one file per county, refreshed nightly",
            fields="nonprofit corporation name; status; address as published in the report",
            coverage=f"all registered nonprofit corporations in {county} County",
            cost_usd="0.00",
            terms="Free public download, no login, no payment, no CAPTCHA. The "
                  "download is a POST carrying a per-session anti-forgery token, so "
                  "it is an interactive control: one operator click, not a script.",
            last_checked=CHECKED,
            blocking_reason="",
            operator_steps=f"Open the report page, click '{county} (Excel)', save the "
                           "file, and transcribe matching organizations into "
                           "sos_import_template.csv. One click per county; seven "
                           "clicks covers the whole CURRENT footprint.",
        ))
    return out


def all_sources() -> List[SourceEntry]:
    return estate_sources() + entity_sources()


SOURCE_MATRIX_COLUMNS = [
    "county", "source_family", "official_source", "source_type", "locator",
    "automatable", "manual", "acquisition", "fmt", "fields", "coverage",
    "cost_usd", "terms", "blocking_reason", "operator_steps", "last_checked",
]
