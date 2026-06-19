#!/usr/bin/env python3
"""derive.py — raw parcels -> scored-input rows.
Filters to HOMES (improvement value>0). Emits full situs+mailing fields and a
homestead flag. Absentee classification is done by the ENGINE's score_absentee
module in score.py (one correct, tested classifier) -- NOT here."""
import csv, json, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# First-3-digit ZIP -> USPS state (inclusive ranges). The county GIS leaves
# MAILINGSTA blank, so we resolve the OWNER MAILING state from its ZIP -- the
# absentee tier logic needs it to tell out-of-state from out-of-county.
_ZIP3 = [
 (5,5,"NY"),(6,9,"PR"),(10,27,"MA"),(28,29,"RI"),(30,38,"NH"),(39,49,"ME"),
 (50,59,"VT"),(60,69,"CT"),(70,89,"NJ"),(100,149,"NY"),(150,196,"PA"),
 (197,199,"DE"),(200,205,"DC"),(206,219,"MD"),(220,246,"VA"),(247,268,"WV"),
 (270,289,"NC"),(290,299,"SC"),(300,319,"GA"),(320,349,"FL"),(350,369,"AL"),
 (370,385,"TN"),(386,397,"MS"),(398,399,"GA"),(400,427,"KY"),(430,459,"OH"),
 (460,479,"IN"),(480,499,"MI"),(500,528,"IA"),(530,549,"WI"),(550,567,"MN"),
 (570,577,"SD"),(580,588,"ND"),(590,599,"MT"),(600,629,"IL"),(630,658,"MO"),
 (660,679,"KS"),(680,693,"NE"),(700,714,"LA"),(716,729,"AR"),(730,749,"OK"),
 (750,799,"TX"),(800,816,"CO"),(820,831,"WY"),(832,838,"ID"),(840,847,"UT"),
 (850,865,"AZ"),(870,884,"NM"),(889,898,"NV"),(900,961,"CA"),(967,968,"HI"),
 (970,979,"OR"),(980,994,"WA"),(995,999,"AK"),
]

def zip_to_state(z):
    z = (z or "").strip()[:5]
    if len(z) < 3 or not z[:3].isdigit():
        return ""
    p = int(z[:3])
    for lo, hi, st in _ZIP3:
        if lo <= p <= hi:
            return st
    return ""

def load_config():
    with open(os.path.join(ROOT, "config.json")) as f: return json.load(f)
def to_float(s):
    try: return float(str(s).replace(",", "").replace("$", "").strip() or 0)
    except ValueError: return 0.0
def norm(s): return (s or "").strip().upper()

def main():
    if len(sys.argv) < 2:
        print("usage: python3 derive.py <raw_csv> [county_key]"); sys.exit(1)
    raw_path = sys.argv[1]; cfg = load_config()
    county = sys.argv[2] if len(sys.argv) > 2 else \
        next((c for c in cfg["counties"] if os.path.basename(raw_path).startswith(c)), None)
    fmap = cfg["counties"][county]["fields"]
    hs_fields = fmap.get("homestead_flags", [])
    def g(row, key):
        col = fmap.get(key); return row.get(col, "") if col else ""
    out = []
    with open(raw_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            imp = to_float(g(row, "improvement_value"))
            if imp <= 0:
                continue  # raw land -> skip, we want HOMES
            mzip = (g(row, "mail_zip") or "").strip()
            mstate = (g(row, "mail_state") or "").strip() or zip_to_state(mzip)
            hs = any(norm(row.get(h, "")) not in ("", "0", "NONE", "N") for h in hs_fields)
            out.append({
                "parcel_id": g(row, "parcel_id"),
                "owner_name": g(row, "owner_name"),
                "situs_address": g(row, "prop_addr"),
                "situs_city": g(row, "prop_city"),
                "situs_state": (g(row, "prop_state") or "IN").strip(),
                "situs_zip": (g(row, "prop_zip") or "").strip(),
                "owner_mailing_address": g(row, "mail_addr"),
                "owner_mailing_city": g(row, "mail_city"),
                "owner_mailing_state": mstate,
                "owner_mailing_zip": mzip,
                "land_value": to_float(g(row, "land_value")),
                "improvement_value": imp,
                "acreage": to_float(g(row, "acreage")),
                "land_use_class": g(row, "class_code"),
                "sale_date": g(row, "sale_date"),
                "homestead_present": hs,
            })
    out_dir = os.path.join(ROOT, "data", "input"); os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.basename(raw_path).replace(".csv", "_input.csv"))
    with open(path, "w", newline="", encoding="utf-8") as f:
        if out:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
    print(f"{len(out)} homes -> {path}")

if __name__ == "__main__": main()
