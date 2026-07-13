#!/usr/bin/env python3
"""Senior-Tenure Lakefront Assemblage — signal #50 + cluster recipe.

Usage: .venv/bin/python senior_assemblage.py config/senior_assemblage.yaml --jurisdiction laporte_in
Emits: out/<runid>/clusters.csv, cluster_parcels.csv, run_manifest.json (SHA-256 provenance).
Never writes to Supabase. sync_to_crm.py remains the single writer.

Deviations from BUILD SPEC v1.0 §6 (each forced by verified data reality,
recorded in the run manifest):
  D1 renormalization — score is scaled by the max points actually achievable
     from the components present (spec §4.4 promised "re-normalizes weights
     automatically"; the §6 listing didn't, and with over65 unavailable the
     55 threshold could never be reached).
  D2 tenure floor — parcels with no tenure-resetting sale inside the SDF
     window (2008->asof) get jurisdiction-config tenure_floor_points instead
     of 0 (window-dark means held >=18y, not held 0y).
  D3 entity exclusion — exclude_entities: owner_kind=='entity' scores 0
     (a utility can't be a senior; window-dark alone would otherwise qualify
     NIPSCO). Trusts are NOT entities here — the surname carries.
  D4 richer outputs — cluster_parcels.csv adds owner_kind/occupancy/floor/
     mailing/cluster columns the caller desk needs; spec columns unchanged.
"""
import argparse, hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml
from libpysal.weights import Rook
from shapely.geometry import box

TRUTHY = ["1", "true", "True", "TRUE", "T", "t", "Y", "y", "yes", "YES"]
SURNAME_STOPWORDS = {"THE", "A", "AN"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_apn(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace(r"[^0-9A-Za-z]", "", regex=True).str.upper()


def first_surname(owner: str) -> str:
    # County rolls are typically "LAST FIRST [& SPOUSE]" — first non-stopword
    # alpha token is the surname (also right for "SMITH FAMILY TRUST").
    toks = [t.upper() for t in re.findall(r"[A-Za-z]+", str(owner))]
    toks = [t for t in toks if t not in SURNAME_STOPWORDS]
    return toks[0] if toks else ""


def tenure_points(years: pd.Series, ladder) -> pd.Series:
    pts = pd.Series(0.0, index=years.index)
    for min_years, points in ladder:            # ladder is sorted high->low in YAML
        pts = pts.where(~((years >= min_years) & (pts == 0)), points)
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--jurisdiction", required=True)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    j = cfg["jurisdictions"][args.jurisdiction]
    asof = pd.Timestamp(cfg["asof_date"])
    runid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{args.jurisdiction}"
    outdir = Path("out") / runid
    outdir.mkdir(parents=True, exist_ok=True)
    gates = {}

    # ---- LOAD ----
    geo = gpd.read_file(j["parcel_file"]).to_crs(cfg["crs"])
    geo["geometry"] = geo.geometry.buffer(0)                     # heal invalid rings
    geo["apn"] = norm_apn(geo[j["apn_field_geo"]])
    geo = geo[~geo["apn"].isin(["", "NONE", "NAN", "NULL"])]
    # one row per APN: dissolve duplicate-APN polygons (condo stacks, multi-part
    # parcels) so a single parcel can never masquerade as a 2-parcel assemblage
    dup = geo["apn"].duplicated(keep=False)
    if dup.any():
        merged = geo[dup].dissolve(by="apn", as_index=False, aggfunc="first")
        geo = pd.concat([geo[~dup], merged], ignore_index=True)
        gates["geo_apn_dissolved"] = int(dup.sum())
    gates["parcels_geo"] = len(geo)

    attrs = pd.read_csv(j["attr_file"], dtype=str, low_memory=False)
    attrs["apn"] = norm_apn(attrs[j["apn_field_attr"]])
    gates["parcels_attr"] = len(attrs)

    df = geo.merge(attrs, on="apn", how="inner", suffixes=("_geo", ""))
    gates["joined"] = len(df)
    match_rate = gates["joined"] / max(gates["parcels_attr"], 1)
    print(f"[GATE] APN join: {gates['joined']}/{gates['parcels_attr']} = {match_rate:.1%}")
    if match_rate < 0.95:
        sys.exit("[HALT] Join rate <95% — inspect APN formats before proceeding.")

    # ---- LAKE PROXIMITY ----
    lakes = gpd.read_file(j["lake_file"]).to_crs(cfg["crs"])
    if j.get("lake_names"):
        lakes = lakes[lakes[j["lake_name_field"]].isin(j["lake_names"])]
    if lakes.empty:
        sys.exit("[HALT] No lake polygons matched lake_names — check names/field.")
    lake_geom = lakes.union_all()
    # perf: clip the lake to the parcel extent (+5km). At a 300m buffer nothing
    # beyond that pad can matter, and NHD ships the ENTIRE Lake Michigan polygon.
    minx, miny, maxx, maxy = df.total_bounds
    pad = max(float(cfg["lake_buffer_m"]), 5000.0)
    lake_geom = lake_geom.intersection(box(minx - pad, miny - pad, maxx + pad, maxy + pad))
    if lake_geom.is_empty:
        sys.exit("[HALT] No lake geometry within 5km of the parcel extent — check lake_names/CRS.")
    df["dist_to_lake_m"] = df.geometry.distance(lake_geom)
    df["lake_adjacent"] = df["dist_to_lake_m"] <= cfg["lake_buffer_m"]
    gates["lake_adjacent"] = int(df["lake_adjacent"].sum())
    print(f"[GATE] Lake-adjacent (<={cfg['lake_buffer_m']}m): {gates['lake_adjacent']}")

    # ---- SIGNAL #50 (jurisdiction-aware, renormalized — D1/D2/D3) ----
    w = j["weights"]
    sale = pd.to_datetime(df[j["sale_date_field"]], errors="coerce")
    df["tenure_years"] = (asof - sale).dt.days / 365.25
    score = tenure_points(df["tenure_years"].fillna(0), w["tenure"])
    avail = float(max(p for _, p in w["tenure"]))
    floor_pts = float(j.get("tenure_floor_points", 0))
    if floor_pts and "tenure_floor" in df:                       # D2
        is_floor = df["tenure_floor"].isin(TRUTHY) & sale.isna()
        score = score.where(~is_floor, floor_pts)
        gates["tenure_floor_rows"] = int(is_floor.sum())
    if j["state"] == "IN":
        if j.get("over65_available"):
            score += df[j["over65_field"]].fillna("0").isin(TRUTHY) * w["over65"]
            score += df[j["circuit_breaker_field"]].fillna("0").isin(TRUTHY) * w["circuit_breaker"]
            avail += w["over65"] + w["circuit_breaker"]
        if j["homestead_field"] in df:
            score += df[j["homestead_field"]].fillna("0").isin(TRUTHY) * w["homestead"]
            avail += w["homestead"]
    else:  # MI — TV/SEV uncapping gap
        sev = pd.to_numeric(df[j["sev_field"]], errors="coerce")
        tv = pd.to_numeric(df[j["tv_field"]], errors="coerce")
        ratio = (tv / sev).where(sev > 0)
        bin_pts = pd.Series(0.0, index=df.index)
        for ceiling, points in w["tv_sev_bins"]:                 # sorted low->high in YAML
            bin_pts = bin_pts.where(~((ratio <= ceiling) & (bin_pts == 0)), points)
        score += bin_pts
        avail += float(max(p for _, p in w["tv_sev_bins"]))
        if j.get("pre_field") and j["pre_field"] in df:
            pre = pd.to_numeric(df[j["pre_field"]], errors="coerce").fillna(0)
            score += (pre >= 100) * w["pre"]
            avail += w["pre"]
    # probate surname bonus (both states)
    surn_path = Path(cfg["probate_surnames_file"])
    df["surname"] = df[j["owner_field"]].map(first_surname)
    if surn_path.exists() and surn_path.stat().st_size > 0:
        probate = set(pd.read_csv(surn_path)["surname"].str.upper())
        score += df["surname"].isin(probate) * w["probate_surname"]
        avail += w["probate_surname"]
    df["senior_score"] = (score * (100.0 / avail)).clip(upper=100)  # D1
    gates["score_avail_max"] = avail
    if j.get("exclude_entities") and "owner_kind" in df:         # D3
        ent = df["owner_kind"].eq("entity")
        df.loc[ent, "senior_score"] = 0.0
        df.loc[ent, "surname"] = ""
        gates["entities_excluded"] = int(ent.sum())
    gates["qualifying"] = int(((df.senior_score >= cfg["qualify_threshold"]) & df.lake_adjacent).sum())
    print(f"[GATE] Qualifying (score>={cfg['qualify_threshold']} + lake): {gates['qualifying']}")

    # ---- ASSEMBLAGE: Rook contiguity -> connected components ----
    qual = df[(df.senior_score >= cfg["qualify_threshold"]) & df.lake_adjacent].reset_index(drop=True)
    if len(qual) == 0:
        sys.exit("[HALT] Zero qualifying parcels — lower threshold or widen buffer and re-run.")
    rw = Rook.from_dataframe(qual, use_index=False, silence_warnings=True)
    qual["cluster_id"] = rw.component_labels
    acres = pd.to_numeric(qual[j["acres_field"]], errors="coerce").fillna(0)
    # county ACREAGE fields are sparse on platted lots — fall back to polygon area
    qual["_acres"] = acres.where(acres > 0, qual.geometry.area * 0.000247105)
    agg = qual.groupby("cluster_id").agg(
        n_parcels=("apn", "count"),
        total_acres=("_acres", "sum"),
        n_surnames=("surname", "nunique"),
        surnames=("surname", lambda s: "|".join(sorted(set(x for x in s if x)))),
        mean_score=("senior_score", "mean"),
    ).reset_index()
    agg["same_surname_family"] = ((agg.n_parcels >= 2) & (agg.n_surnames == 1)
                                  & (agg.surnames.str.len() > 0))
    clusters = agg[agg.n_parcels >= cfg["min_cluster_size"]].sort_values(
        ["same_surname_family", "n_parcels", "total_acres"], ascending=False)
    gates["clusters"] = len(clusters)
    gates["family_clusters"] = int(clusters.same_surname_family.sum())
    print(f"[GATE] Clusters >={cfg['min_cluster_size']}: {gates['clusters']} "
          f"(family-surname clusters: {gates['family_clusters']})")

    # ---- OUTPUTS + PROVENANCE ----
    keep = qual[qual.cluster_id.isin(clusters.cluster_id)]
    cent = keep.geometry.centroid.to_crs("EPSG:4326")
    parcels_out = pd.DataFrame({
        "parcel_apn": keep[j["apn_field_attr"]] if j["apn_field_attr"] in keep else keep.apn,
        "county_fips": j["county_fips"],
        "owner_name": keep[j["owner_field"]], "senior_score": keep.senior_score.round(1),
        "tenure_years": keep.tenure_years.round(1), "dist_to_lake_m": keep.dist_to_lake_m.round(0),
        "cluster_id": keep.cluster_id, "situs_lat": cent.y.round(6), "situs_lng": cent.x.round(6),
        "signal_tags": "senior_tenure|lake_proximity|assemblage",
        "recipe_id": cfg["recipe_id"],
    })
    for extra in ("owner_kind", "owner_occupied_proxy", "tenure_floor", "situs_address",
                  "owner_mailing_address", "owner_mailing_city", "owner_mailing_state",
                  "owner_mailing_zip", "surname"):                # D4
        if extra in keep:
            parcels_out[extra] = keep[extra].values
    parcels_out["cluster_size"] = keep.cluster_id.map(clusters.set_index("cluster_id").n_parcels).values
    parcels_out["same_surname_family"] = keep.cluster_id.map(
        clusters.set_index("cluster_id").same_surname_family).values
    parcels_out = parcels_out.sort_values(["same_surname_family", "cluster_size", "cluster_id"],
                                          ascending=[False, False, True])
    parcels_out.to_csv(outdir / "cluster_parcels.csv", index=False)
    clusters.to_csv(outdir / "clusters.csv", index=False)
    manifest = {
        "recipe_id": cfg["recipe_id"], "jurisdiction": args.jurisdiction, "run_id": runid,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "config_sha256": sha256_file(Path(args.config)),
        "inputs": {str(p): sha256_file(Path(p)) for p in
                   [j["parcel_file"], j["attr_file"], j["lake_file"]] if Path(p).exists()},
        "gates": gates, "params": {k: cfg[k] for k in
                   ["lake_buffer_m", "qualify_threshold", "min_cluster_size", "asof_date"]},
        "deviations": ["D1 renormalized score (avail=%s)" % avail,
                       "D2 tenure floor %s pts for SDF-window-dark parcels" % floor_pts,
                       "D3 entity exclusion (trusts kept)",
                       "D4 additive output columns"],
        "attribution": "Indiana Sales Disclosure data, DLGF via STATS Indiana (IU IBRC); "
                       "parcels: LaPorte County GIS (AGOL); hydro: USGS NHD",
    }
    (outdir / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[DONE] {outdir}/  — clusters.csv, cluster_parcels.csv, run_manifest.json")


if __name__ == "__main__":
    main()
