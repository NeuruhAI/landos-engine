#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.nc_dps_wave_a_pipeline import build


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "contracts/nc_dps_property_packet.v1.schema.json",
    )
    args = parser.parse_args()
    print(json.dumps(build(args.signals, args.output, args.schema), indent=2))


if __name__ == "__main__":
    main()
