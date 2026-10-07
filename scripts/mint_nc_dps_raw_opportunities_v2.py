#!/usr/bin/env python3
from __future__ import annotations

# The canonical raw-opportunity importer intentionally emits source_type
# operator_raw_opportunity. Add that source type to the existing raw writer so
# DeedSonar-shaped raw imports survive the LandOS intake proof unchanged.
from app import nc_dps_raw_writer as raw_writer

raw_writer.RAW_OPPORTUNITY_TYPES.add("operator_raw_opportunity")

from scripts import mint_nc_dps_raw_opportunities as base  # noqa: E402


if __name__ == "__main__":
    base.main()
