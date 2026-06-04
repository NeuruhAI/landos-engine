import os, sys, csv, json, random, urllib.request
from dotenv import load_dotenv
load_dotenv()
BASE = f"http://localhost:{os.environ.get('PORT','8400')}"
SECRET = os.environ["LANDOS_SECRET"]
TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
COUNTIES = [("St. Joseph","IN"),("Elkhart","IN"),("Berrien","MI")]
def _post(path, payload):
    req = urllib.request.Request(BASE+path, data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json","X-LANDOS-SECRET":SECRET}, method="POST")
    with urllib.request.urlopen(req) as r: return json.load(r)
def _get(path):
    req = urllib.request.Request(BASE+path, headers={"X-LANDOS-SECRET":SECRET})
    with urllib.request.urlopen(req) as r: return json.load(r)
def _b(v): return str(v).strip().lower() in ("1","true","yes","y","t")
def _gen(i):
    county, state = random.choice(COUNTIES)
    return {"tenant_id":TENANT,"parcel_apn":f"DEMO-{state}-{1000+i}","owner_hash":f"demo-owner-{i:04d}",
        "county":county,"state":state,
        "distress":{"tax_delinquent":random.random()<0.5,"probate":random.random()<0.35,"pre_foreclosure":random.random()<0.2,"code_violation":random.random()<0.25,"vacancy":random.random()<0.4,"long_hold":random.random()<0.5},
        "owner":{"is_entity":random.random()<0.3,"age_band":random.choice(["senior","boomer","other","other"]),"out_of_state":random.random()<0.45,"no_succession":random.random()<0.3},
        "arbitrage":{"list_price_per_acre":random.choice([2000,3000,4000,5000,6000]),"market_price_per_acre":random.choice([6000,7000,8000,9000]),"acreage":random.choice([10,20,40,80,120]),"subdivisions_to_2x":random.randint(1,6),"carry_feasible_90d":random.random()<0.6}}
def _from_csv(row):
    return {"tenant_id":TENANT,"parcel_apn":row.get("parcel_apn") or row.get("apn") or "","owner_hash":row.get("owner_hash") or row.get("owner") or "",
        "county":row.get("county"),"state":row.get("state"),
        "distress":{k:_b(row.get(k,"")) for k in ["tax_delinquent","probate","pre_foreclosure","code_violation","vacancy","long_hold"]},
        "owner":{"is_entity":_b(row.get("is_entity","")),"age_band":(row.get("age_band") or "other").strip().lower(),"out_of_state":_b(row.get("out_of_state","")),"no_succession":_b(row.get("no_succession",""))},
        "arbitrage":{"list_price_per_acre":float(row.get("list_price_per_acre") or 0),"market_price_per_acre":float(row.get("market_price_per_acre") or 0),"acreage":float(row.get("acreage") or 0),"subdivisions_to_2x":int(float(row.get("subdivisions_to_2x") or 10)),"carry_feasible_90d":_b(row.get("carry_feasible_90d",""))}}
def main():
    args = sys.argv[1:]
    if args and args[0]=="--demo":
        n = int(args[1]) if len(args)>1 else 20
        parcels = [_gen(i) for i in range(n)]; print(f"Generating {n} demo parcels and scoring each...\n")
    elif args:
        with open(args[0]) as f: parcels = [_from_csv(r) for r in csv.DictReader(f)]
        print(f"Loaded {len(parcels)} parcels from {args[0]}, scoring...\n")
    else:
        print("Usage: python -m scripts.batch_score --demo 25  |  python -m scripts.batch_score file.csv"); return
    scored = 0
    for p in parcels:
        try: _post("/evaluate", p); scored += 1
        except Exception as e: print("  skipped:", e)
    print(f"Scored {scored} parcels.\n")
    q = _get(f"/leads/top?tenant_id={TENANT}&limit=15")
    print(f"================  TOP {q['count']} UNCLAIMED LEADS  ================")
    for i, lead in enumerate(q["leads"], 1):
        facts = ", ".join(f["factor"] for f in lead["top_factors"])
        print(f"{i:2}.  score {lead['score']:>5}   {lead['county']:<10} {lead['state']:<2}   {lead['parcel_apn']:<14}  [{facts}]")
    print("="*64)
if __name__ == "__main__": main()
