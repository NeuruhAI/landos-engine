#!/usr/bin/env python3
"""Page an ArcGIS FeatureServer layer to a single GeoJSON FeatureCollection.
Stdlib only (runs before the venv exists). Emits <out>.manifest.json with
SHA-256 + page/feature counts (CRUCIBLE-style provenance).

Usage: python3 fetch_parcels.py <query_url> <out.geojson> [page_size]
"""
import hashlib, json, sys, time, urllib.parse, urllib.request

def fetch_page(url, params, tries=4):
    qs = urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(f"{url}?{qs}", timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 — retry any transport/parse error
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"page failed after {tries} tries: {last}")

def main():
    url, out = sys.argv[1], sys.argv[2]
    page = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    count = fetch_page(url, {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
    print(f"[fetch] {count} features from {url}", flush=True)
    written = 0
    with open(out, "w") as f:
        f.write('{"type":"FeatureCollection","features":[')
        first = True
        for offset in range(0, count, page):
            d = fetch_page(url, {
                "where": "1=1", "outFields": "*", "f": "geojson",
                "resultOffset": offset, "resultRecordCount": page,
                "orderByFields": "FID ASC",
            })
            feats = d.get("features", [])
            if not feats and offset < count:
                raise RuntimeError(f"empty page at offset {offset} (format problem?)")
            for feat in feats:
                if not first:
                    f.write(",")
                f.write(json.dumps(feat, separators=(",", ":")))
                first = False
                written += 1
            print(f"[fetch] {written}/{count}", flush=True)
        f.write("]}")
    h = hashlib.sha256()
    with open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    manifest = {"source_url": url, "features": written, "expected": count,
                "sha256": h.hexdigest(), "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(out + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[done] {written} features -> {out}", flush=True)
    if written != count:
        print(f"[WARN] wrote {written} != expected {count}", flush=True)

if __name__ == "__main__":
    main()
