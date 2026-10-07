"""Independent ZIP-locality arbiter for address-variance adjudication.

WHY THIS EXISTS. The NCSBE identity matcher can produce a TIER_C decision in
which the owner address and the voter address agree on house number and street
but disagree on ZIP. That is either the same physical place carrying two ZIP
codes (an assessor/USPS artifact) or two different places. The voter file
cannot answer that question about itself, so this module answers it from a
different authority: the Census ZCTA national gazetteer, a free official
federal file of ZIP-code tabulation areas with land area and a centroid.

Two ZIPs whose centroids sit within a short distance of each other, and of the
subject parcel, describe one locality. That is a geographic fact, established
without consulting the source under test.
"""
from __future__ import annotations

import csv
import io
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_zcta_national.zip"
)
GAZETTEER_FAMILY = "CENSUS_ZCTA_GAZETTEER"

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class Zcta:
    zip5: str
    lat: float
    lon: float
    land_sqmi: float


def _f(value: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def load_gazetteer(path: str | Path) -> Dict[str, Zcta]:
    """Parse the official gazetteer zip (or its extracted .txt) into ZIP -> Zcta."""
    p = Path(path)
    if not p.exists():
        return {}
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                return {}
            payload = zf.read(names[0])
    else:
        payload = p.read_bytes()
    text = payload.decode("utf-8", errors="replace")
    out: Dict[str, Zcta] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        zip5 = clean.get("GEOID") or clean.get("ZCTA5") or ""
        if len(zip5) != 5 or not zip5.isdigit():
            continue
        lat, lon = _f(clean.get("INTPTLAT")), _f(clean.get("INTPTLONG"))
        if not lat or not lon:
            continue
        out[zip5] = Zcta(zip5=zip5, lat=lat, lon=lon, land_sqmi=_f(clean.get("ALAND_SQMI")))
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def zip_separation_km(a: str, b: str, gaz: Dict[str, Zcta]) -> Optional[float]:
    """Centroid separation of two ZIPs, or None if either is unknown."""
    za, zb = gaz.get((a or "").strip()), gaz.get((b or "").strip())
    if not za or not zb:
        return None
    return haversine_km(za.lat, za.lon, zb.lat, zb.lon)


def zip_covers_point(zip5: str, lat: float, lon: float, gaz: Dict[str, Zcta]) -> Optional[float]:
    """Distance in km from a point to a ZIP centroid, or None if ZIP is unknown."""
    z = gaz.get((zip5 or "").strip())
    if not z or not lat or not lon:
        return None
    return haversine_km(z.lat, z.lon, lat, lon)


def zcta_radius_km(zip5: str, gaz: Dict[str, Zcta]) -> Optional[float]:
    """Radius of the equal-area circle for a ZCTA — its natural distance scale."""
    z = gaz.get((zip5 or "").strip())
    if not z or z.land_sqmi <= 0:
        return None
    return math.sqrt((z.land_sqmi * 2.589988) / math.pi)


SAME_LOCALITY = "SAME_LOCALITY"
DIFFERENT_LOCALITY = "DIFFERENT_LOCALITY"
UNKNOWN_LOCALITY = "UNKNOWN_LOCALITY"
# A ZIP with no tabulation area has no residential population — it is a PO Box
# or unique-recipient ZIP. It therefore cannot be a competing physical place;
# it is a mailing artifact sitting on top of some other delivery ZIP.
NON_DELIVERY_ZIP = "NON_DELIVERY_ZIP"


def classify_zip_variance(
    owner_zip: str,
    voter_zip: str,
    gaz: Dict[str, Zcta],
    *,
    parcel_lat: float = 0.0,
    parcel_lon: float = 0.0,
) -> tuple[str, dict]:
    """Decide whether two ZIPs on an otherwise-identical address are one place.

    Same locality requires the ZIP centroids to be closer than the sum of their
    own radii — i.e. the tabulation areas effectively abut or overlap. When the
    parcel's coordinate is known it must also sit within a plausible distance of
    BOTH ZIP centroids, which rules out a same-street/same-house coincidence in a
    distant part of the county.
    """
    detail: dict = {"owner_zip": owner_zip, "voter_zip": voter_zip}
    if owner_zip and voter_zip and owner_zip == voter_zip:
        return SAME_LOCALITY, {**detail, "reason": "identical_zip"}
    owner_known = (owner_zip or "").strip() in gaz
    voter_known = (voter_zip or "").strip() in gaz
    if voter_known and not owner_known:
        return NON_DELIVERY_ZIP, {
            **detail,
            "reason": "owner_zip_has_no_zcta_po_box_or_unique",
        }
    sep = zip_separation_km(owner_zip, voter_zip, gaz)
    if sep is None:
        return UNKNOWN_LOCALITY, {**detail, "reason": "zip_not_in_gazetteer"}
    ra = zcta_radius_km(owner_zip, gaz) or 0.0
    rb = zcta_radius_km(voter_zip, gaz) or 0.0
    detail.update({
        "centroid_separation_km": round(sep, 3),
        "owner_zcta_radius_km": round(ra, 3),
        "voter_zcta_radius_km": round(rb, 3),
    })
    adjacent = sep <= (ra + rb)
    if parcel_lat and parcel_lon:
        da = zip_covers_point(owner_zip, parcel_lat, parcel_lon, gaz)
        db = zip_covers_point(voter_zip, parcel_lat, parcel_lon, gaz)
        detail["parcel_to_owner_zip_km"] = round(da, 3) if da is not None else None
        detail["parcel_to_voter_zip_km"] = round(db, 3) if db is not None else None
        if da is not None and db is not None:
            near = (da <= max(ra, 1.0) * 2) and (db <= max(rb, 1.0) * 2)
            if adjacent and near:
                return SAME_LOCALITY, {**detail, "reason": "abutting_zctas_parcel_within_both"}
            return DIFFERENT_LOCALITY, {
                **detail,
                "reason": "zctas_adjacent_but_parcel_far" if adjacent else "zctas_separated",
            }
    if adjacent:
        return SAME_LOCALITY, {**detail, "reason": "abutting_zctas_no_parcel_point"}
    return DIFFERENT_LOCALITY, {**detail, "reason": "zctas_separated"}
