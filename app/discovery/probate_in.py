"""
probate_in.py — DiscoveryAdapter #1: Indiana probate (St. Joseph County, 18141).

WHY INDIANA FIRST (the cleanest case by design): Ind. Code 29-1-12-1 requires
estate inventories to classify real property "with plat or survey description"
— so a probate signal carries a TWO-KEY match (decedent name + legal
description -> parcel) that defeats the common-name problem.

ACCESS MODE: **va_fed** — ruled by the source's own terms, probed 2026-06-10:
  - Public docket: https://public.courts.in.gov/mycase (Odyssey "MyCase").
    No CAPTCHA observed; search requires a party NAME or case number (no
    public browse-all-recent-filings — that absence is the anti-bulk design).
  - robots.txt DISALLOWS the MyCase app paths and all PDF/TIFF documents.
  - Terms of Use (in.gov/courts/policies/tou-mycase) verbatim: no "spiders,
    robots, avatars, intelligent agents, or any other extraction or navigation
    search except for a normal browser"; copying limited to "the small amount
    of materials and information temporarily required for an ordinary single
    use"; bulk access via a separate process.
  - => automation against MyCase is OUT. The sanctioned automation path is the
    Indiana Office of Court Services **Bulk Data / Data-Use Agreement (DUA)**
    — noted as the E2-era upgrade, NOT assumed here.

THE VA WORKFLOW THIS ADAPTER CONSUMES (documented in DISCOVERY_ADAPTERS.md §3):
  a human works MyCase in a normal browser — searches recent decedents (from
  obituaries / legal notices), opens each estate case (EU/ES/EM), and records
  one CSV row per case into data/incoming/probate_in_18141.csv with columns:

    case_number, case_title, court, case_type, filed_date, status,
    decedent_name, personal_rep, heirs, attorney, source_url,
    legal_description, parcel_apn

  heirs = pipe-separated names exactly as the docket shows them (consent
  filings on unsupervised estates name the heir chain). legal_description /
  parcel_apn come from the §29-1-12-1 inventory WHEN the clerk file is pulled;
  blank when only the docket was read. parcel_apn, when present, MUST be the
  county tax APN (71-form) — never a ReportAll 018-form id.

Timing datum (probed 2026-06-10): obituary -> estate filing lags weeks; both
June-2026 decedents checked had no case yet, so the VA should work obits aged
3+ weeks, and re-check younger ones on the next pass.
"""
from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/probate_in_18141.csv"


class ProbateINStJoeAdapter(DiscoveryAdapter):
    source_type = "probate_in"
    county_fips = "18141"
    access_mode = "va_fed"
    access_path = "https://public.courts.in.gov/mycase (St. Joseph County, Probate filter)"
    recipe_hint = "R4"   # deceased -> heirs; PR is a target inside R4

    def __init__(self, tenant_id: str, csv_path: str = DEFAULT_CSV):
        super().__init__(tenant_id)
        self.csv_path = csv_path

    def fetch(self) -> List[DiscoverySignal]:
        """Read the VA hand-back CSV. Same output shape an automated (bulk-DUA)
        fetch would produce — the pipeline cannot tell the difference."""
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"{self.csv_path} not found — the probate_in adapter is va_fed; "
                "place the VA's docket CSV there (see DISCOVERY_ADAPTERS.md §3)."
            )
        signals: List[DiscoverySignal] = []
        with open(self.csv_path, newline="") as f:
            for row in csv.DictReader(f):
                case = (row.get("case_number") or "").strip()
                if not case:
                    continue
                heirs = [h.strip() for h in (row.get("heirs") or "").split("|") if h.strip()]
                # two-key evidence (legal description / county APN from the
                # inventory) earns higher match confidence than docket-name-only
                conf = 0.85 if (row.get("legal_description") or row.get("parcel_apn")) else 0.6
                signals.append({
                    "source_ref": case,
                    "source_url": (row.get("source_url") or "").strip() or None,
                    "owner_name": (row.get("decedent_name") or "").strip() or None,
                    "signal_date": (row.get("filed_date") or "").strip() or None,
                    "confidence": conf,
                    "parcel_apn": (row.get("parcel_apn") or "").strip() or None,
                    "raw_payload": {**{k: (v or "").strip() for k, v in row.items()},
                                    "heirs_list": heirs},
                })
        return signals
