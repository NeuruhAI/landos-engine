"""
obituary.py — DiscoveryAdapter: obituary / death-notice signals (P-23).

DOCTRINE (docs/probate_feasibility.md, probed 2026-06): an obituary is NAME-ONLY —
it carries NO property link, ever. That makes it a LOW-CONFIDENCE ENRICHMENT source:
useful as the earliest death signal (weeks before the estate filing) and as
corroboration layered onto a property-linked match, NEVER a primary join and NEVER
a minting source. Gate-in-data: signal_type_registry pins obituary as
signal_class='enrichment', mints=false (mig 040) — no pipeline can mint from it.

ACCESS MODE: **va_fed** — Legacy.com (covers the South Bend Tribune / Elkhart Truth /
Herald-Palladium markets) has no API and its ToS prohibits scraping + bot protection;
funeral-home sites are unstructured. A human/VA works the public listings in a normal
browser and records one CSV row per notice into data/incoming/obituary_<fips>.csv:

    source_ref, obituary_url, decedent_name, age, city, state,
    published_date, death_date, surviving_family, funeral_home, county_fips, notes

  surviving_family = pipe-separated names exactly as printed (heir-chain evidence for
  the LATER probate correlation — third-party PII, so the correlation receipt only
  ever exposes a count, never the names).

HARD CONTRACT (the mint-safety line):
  * This adapter NEVER emits parcel_apn — even if a column with that name appears in
    the CSV it is refused, because an obituary cannot carry a canonical property key.
    Any obituary→parcel identity is a FUZZY guess and belongs to the correlation
    module (app/discovery/obituary_correlation.py), which can only produce REVIEW
    candidates — never an automatic lead mint.
  * Confidence is capped at OBIT_CONFIDENCE (0.35): name-only, one join key, severe
    common-name exposure. Confidence only ever RISES in the correlation module, and
    only when a property-linked probate case corroborates.
"""
from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/obituary_<county_fips>.csv"

# Name-only, one join key, no property link: the honest ceiling for a raw obituary.
OBIT_CONFIDENCE = 0.35


class ObituaryAdapter(DiscoveryAdapter):
    source_type = "obituary"
    county_fips = "18141"
    access_mode = "va_fed"
    access_path = ("Legacy.com market pages + local funeral homes, worked by a human in "
                   "a normal browser (ToS: no scraping) -> va_fed CSV hand-back")
    recipe_hint = "R4"   # death -> estate/heir outreach, via the probate correlation

    def __init__(self, tenant_id: str, county_fips: str = "18141", csv_path: str = None):
        super().__init__(tenant_id)
        self.county_fips = county_fips
        self.csv_path = csv_path or DEFAULT_CSV.replace("<county_fips>", county_fips)

    def fetch(self) -> List[DiscoverySignal]:
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"{self.csv_path} not found — obituary is va_fed; place the VA's "
                "curated CSV there (see the module docstring for columns)."
            )
        signals: List[DiscoverySignal] = []
        with open(self.csv_path, newline="") as f:
            for row in csv.DictReader(f):
                ref = (row.get("source_ref") or "").strip()
                name = (row.get("decedent_name") or "").strip()
                if not ref or not name:
                    continue   # a notice without a ref or a name is unusable — skip, never guess
                family = [h.strip() for h in (row.get("surviving_family") or "").split("|")
                          if h.strip()]
                signals.append({
                    "source_ref": ref,
                    "source_url": (row.get("obituary_url") or "").strip() or None,
                    "owner_name": name,   # the DECEDENT — a candidate identity, not a match
                    "signal_date": ((row.get("death_date") or "").strip()
                                    or (row.get("published_date") or "").strip() or None),
                    "confidence": OBIT_CONFIDENCE,
                    # HARD CONTRACT: no parcel_apn, ever — an obituary has no property link.
                    "parcel_apn": None,
                    "raw_payload": {
                        **{k: (v or "").strip() for k, v in row.items()
                           if k not in ("parcel_apn",)},
                        "surviving_family_list": family,
                        "name_only": True,   # explicit: one join key, fuzzy-identity territory
                    },
                })
        return signals


class ObituaryStJoeINAdapter(ObituaryAdapter):
    county_fips = "18141"


class ObituaryBerrienMIAdapter(ObituaryAdapter):
    county_fips = "26021"
