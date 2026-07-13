#!/usr/bin/env python3
"""Build data/in/laporte_scored.csv — the attribute side of the Track A join.

Sources:
  1. laporte_parcels.geojson properties (county AGOL layer): APN, owner,
     mailing, acreage, class, situs. Attributes ride the same file as the
     geometry, so the pipeline's APN join is internally consistent.
  2. sdf_sales_multi_year.csv (STATS Indiana SDF 2008->2026): last
     TENURE-RESETTING conveyance per parcel.

Tenure-reset semantics (documented, calibratable):
  resets  = arm's-length (price>0 AND not quitclaim) OR sheriff sale
  ignored = quitclaims & $0 transfers (family paper moves — family tenure,
            which is what an assemblage hunt cares about, does not reset)
  no reset on record => tenure_floor=1 (held since before the 2008 window)

Owner-occupancy proxy: normalized mailing street == normalized situs street
(county layer has no homestead/deduction fields; swap in real deduction data
when it lands and flip over65_available in the YAML).

Owner kind: 'entity' (LLC/INC/utility/gov/bank...) vs 'human'. Trusts stay
'human' — SMITH FAMILY TRUST is precisely this recipe's target profile.

Usage: .venv/bin/python build_attrs_laporte.py
"""
import json, re
from pathlib import Path

import pandas as pd
import pyogrio

ROOT = Path(__file__).parent
GEO = ROOT / "data/in/laporte_parcels.geojson"
SDF = ROOT / "data/in/sdf_sales_multi_year.csv"
OUT = ROOT / "data/in/laporte_scored.csv"

TRUTHY = {"1", "true", "t", "y", "yes"}

ENTITY_PAT = re.compile(
    r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO\b|LP|LLP|LTD|"
    r"CITY OF|TOWN OF|COUNTY|STATE OF|UNITED STATES|USA\b|SCHOOL|CHURCH|PARISH|"
    r"DIOCESE|MINISTRIES|ASSOCIATION|ASSN|AUTHORITY|DISTRICT|COMMISSION|DEPT|"
    r"DEPARTMENT|BANK|CREDIT UNION|PARK|CEMETERY|LODGE|CLUB|FOUNDATION|"
    r"PARTNERS|PARTNERSHIP|PROPERTIES|HOLDINGS|INVESTMENTS|ENTERPRISES|"
    r"CONSERVANCY|UTILITY|SERVICE COMPANY|RAILROAD|RAILWAY|RR|PIPELINE|NIPSCO|"
    r"PUBLIC SERVICE|REALTY|DEVELOPMENT|CONDOMINIUM|HOMEOWNERS|HOA|COOPERATIVE|"
    r"TOWNSHIP|TWP|LIBRARY|HOSPITAL|REDEVELOPMENT|CONSERVATION|AMERICAN LEGION|VFW)\b", re.I)
TRUST_PAT = re.compile(r"\bTRUST\b|\bTR\b|\bTTEE\b|\bTRUSTEE", re.I)
# institutional trustees (Indiana land-trust vehicles): unknown beneficiary ->
# can't be scored as a senior; the pierce lane can unmask them later
INSTITUTIONAL_TRUSTEE_PAT = re.compile(r"TRUST\s+(COMPANY|CO\b)|TITLE\s+(COMPANY|CO\b)|\bBANK\b", re.I)


def norm_street(s):
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z ]", " ", str(s or ""))).strip().upper()


def owner_kind(name):
    n = str(name or "")
    if INSTITUTIONAL_TRUSTEE_PAT.search(n):
        return "entity"
    if TRUST_PAT.search(n):
        return "human"          # family trusts carry the surname — keep
    return "entity" if ENTITY_PAT.search(n) else "human"


def main():
    attrs = pyogrio.read_dataframe(GEO, read_geometry=False)
    print(f"[attrs] {len(attrs)} parcel rows from layer")
    df = pd.DataFrame({
        "parcel_apn": attrs["parcel_num"].astype(str),
        "owner_name": attrs["Owner_Name"].astype(str).str.strip(),
        "situs_address": attrs["Property_A"].astype(str).str.strip(),
        "owner_mailing_address": attrs["Owner_Addr"].astype(str).str.strip(),
        "owner_mailing_city": attrs["Owner_City"].astype(str).str.strip(),
        "owner_mailing_state": attrs["Owner_Stat"].astype(str).str.strip(),
        "owner_mailing_zip": attrs["Owner_ZIP"].astype(str).str.strip(),
        "acres": attrs["ACREAGE"],
        "class_code": attrs["LABEL_CLAS"],
        "tax_unit": attrs["tax_unit"].astype(str),
    })
    df = df[df.parcel_apn.notna() & ~df.parcel_apn.str.strip().str.lower().isin(["", "nan", "none"])]
    df["apn_norm"] = df.parcel_apn.str.replace(r"[^0-9A-Za-z]", "", regex=True).str.upper()
    # the layer carries one row per parcel polygon; dedupe defensively on APN
    df = df.sort_values("parcel_apn").drop_duplicates("apn_norm", keep="first")

    df["owner_kind"] = df.owner_name.map(owner_kind)
    situs, mail = df.situs_address.map(norm_street), df.owner_mailing_address.map(norm_street)
    df["owner_occupied_proxy"] = ((situs != "") & (situs == mail)).astype(int)

    sdf = pd.read_csv(SDF, dtype=str, low_memory=False)
    sdf = sdf[sdf.parcel_number.notna()]
    sdf["apn_norm"] = sdf.parcel_number.str.replace(r"[^0-9A-Za-z]", "", regex=True).str.upper()
    sdf = sdf[sdf.apn_norm.str.startswith("46")]        # LaPorte only for this builder
    sdf["dt"] = pd.to_datetime(sdf.conveyance_date, errors="coerce", format="mixed")
    price = pd.to_numeric(sdf.sale_price, errors="coerce").fillna(0)
    quit_ = sdf.quitclaim.fillna("").str.strip().str.lower().isin(TRUTHY)
    sher = sdf.sheriff_sale.fillna("").str.strip().str.lower().isin(TRUTHY)
    resets = sdf[((price > 0) & ~quit_) | sher].dropna(subset=["dt"])
    last_reset = resets.groupby("apn_norm")["dt"].max()
    print(f"[sdf] {len(sdf)} LaPorte sale rows -> {len(resets)} tenure-resetting -> "
          f"{last_reset.size} distinct parcels")

    df["last_sale_date"] = df.apn_norm.map(last_reset).dt.strftime("%Y-%m-%d")
    df["tenure_floor"] = df.last_sale_date.isna().astype(int)
    df.loc[df.last_sale_date.isna(), "last_sale_date"] = ""

    matched = int((df.tenure_floor == 0).sum())
    print(f"[coverage] parcels: {len(df)} | sdf-matched (tenure known): {matched} "
          f"({matched/len(df):.1%}) | window-dark (floor): {int(df.tenure_floor.sum())}")
    print(f"[coverage] owner_occupied_proxy: {int(df.owner_occupied_proxy.sum())} | "
          f"entities: {int((df.owner_kind=='entity').sum())}")

    df.drop(columns=["apn_norm"]).to_csv(OUT, index=False)
    meta = {
        "rows": len(df), "sdf_rows_laporte": int(len(sdf)),
        "tenure_known": matched, "tenure_floor": int(df.tenure_floor.sum()),
        "owner_occupied_proxy": int(df.owner_occupied_proxy.sum()),
        "entities": int((df.owner_kind == "entity").sum()),
        "reset_semantics": "arms-length (price>0 & !quitclaim) OR sheriff; quitclaim/$0 = family paper, no reset",
    }
    (OUT.parent / "laporte_scored.meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[done] {OUT}")


if __name__ == "__main__":
    main()
