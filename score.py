#!/usr/bin/env python3
"""score.py — rank pulled parcels with the ENGINE'S OWN absentee scorer.
Imports app.score_absentee from landos-engine (pure: no DB, no network), so we
use one correct, tested classifier. Writes a ranked CSV. Writes NOTHING to the DB.
Indiana Standard Homestead Deduction => principal residence => not an absentee lead."""
import csv, json, os, sys, datetime
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))

def load_config():
    with open(os.path.join(ROOT, "config.json")) as f: return json.load(f)

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python3 score.py <input_csv>"); sys.exit(1)
    ip = args[0]; cfg = load_config()

    engine_path = os.path.expanduser(cfg["landos_engine_path"])
    sys.path.insert(0, engine_path)
    try:
        from app.score_absentee import score_lead, COUNTY_GEO
    except Exception as e:
        print(f"could not import score_absentee from {engine_path}: {e}")
        print("Check landos_engine_path in config.json points at the engine repo root.")
        sys.exit(1)

    county = next((c for c in cfg["counties"] if os.path.basename(ip).startswith(c)), None)
    fips = cfg["counties"][county]["county_fips"]
    geo = COUNTY_GEO.get(fips)
    if not geo:
        print(f"warning: COUNTY_GEO has no entry for FIPS {fips} -- tiers degrade to out_of_county.")

    with open(ip, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no input rows"); sys.exit(0)

    scored = []
    for r in rows:
        lead = {
            "owner_name": r.get("owner_name"),
            "situs_address": r.get("situs_address"), "situs_city": r.get("situs_city"),
            "situs_county": geo["name"] if geo else None, "situs_state": r.get("situs_state"),
            "situs_zip": r.get("situs_zip"),
            "owner_mailing_address": r.get("owner_mailing_address"),
            "owner_mailing_city": r.get("owner_mailing_city"),
            "owner_mailing_state": r.get("owner_mailing_state"),
            "owner_mailing_zip": r.get("owner_mailing_zip"),
        }
        res = score_lead(lead, [], oz_hit=None, county_geo=geo)
        homestead = str(r.get("homestead_present", "")).lower() in ("true", "1", "yes")
        if homestead:                       # principal-residence deduction => owner lives there
            res["absentee_tier"] = "owner_occupied"
            res["absentee_flag"] = False
        is_lead = bool(res["absentee_flag"]) and not homestead
        scored.append({**r, **{
            "owner_type": res["owner_type"],
            "absentee_tier": res["absentee_tier"],
            "absentee_flag": res["absentee_flag"],
            "landos_score": res["absentee_score"],
            "fused_score": res["raw_fused_score"],
            "is_lead": is_lead,
        }})

    scored.sort(key=lambda r: (1 if r["is_lead"] else 0, float(r.get("fused_score") or 0)), reverse=True)
    stamp = datetime.date.today().isoformat()
    out_dir = os.path.join(ROOT, "data", "leads"); os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.basename(ip).replace("_input.csv", f"_ranked_{stamp}.csv"))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(scored[0].keys())); w.writeheader(); w.writerows(scored)

    leads = [r for r in scored if r["is_lead"]]
    tiers = Counter(r["absentee_tier"] for r in leads)
    print(f"\nRANKED: {len(scored)} homes, {len(leads)} genuine absentee leads -> {path}")
    print("lead tiers:", dict(tiers))
    print("Top 10:")
    for r in leads[:10]:
        print(f"  {r['landos_score']:>5}  {r['absentee_tier']:<14} "
              f"{r.get('situs_address','')}, {r.get('situs_city','')}  "
              f"<- mails to {r.get('owner_mailing_city','')} {r.get('owner_mailing_state','')}")

if __name__ == "__main__": main()
