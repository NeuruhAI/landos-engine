#!/usr/bin/env python3
"""Build the LaPorte/St. Joseph tenure meter from Indiana SDF archives.

Two source eras, verified 2026-07-12 by direct zip inspection:
  2008-2020  SDF_View_CSV_YYYY.zip — ONE latin-1 pipe-delimited .txt; columns
             Parcel1..Parcel3 (multi-parcel sales), Conveyance_Date,
             C6_Sales_Price, Quitclaim, Foreclosure, County_ID.
  2021+      SDF_YYYY.zip — UTF-16LE tab-delimited SALEDISC.txt (SDF_ID,
             County_ID/Name, C7_Conveyance_Date, E1_Sales_Price,
             C1_Sheriff_Sale, C3_Quitclaim) joined on SDF_ID to
             SALEPARCEL.txt (A1_Parcel_Number, 18-digit state format).

County filter: normalized parcel prefix 46 (LaPorte) / 71 (St. Joseph),
OR County_ID 46/71, OR county name contains porte/joseph — belt and braces
across both eras' formats (2008 parcels are dashed, 2015+ undashed).

Old-era Foreclosure is emitted in the sheriff_sale column: both mean an
involuntary transfer, which RESETS tenure downstream.

ToU: stats.indiana.edu /sdfdata/ permitted (ruled in landos-ingest manifest
sdf_sales_2024.json; attribution: Indiana DLGF via STATS Indiana / IU IBRC).

Usage: python3 fetch_sdf_tenure.py <out_csv>
"""
import csv, hashlib, json, os, re, sys, time, urllib.request, zipfile

YEARS = list(range(2008, 2027))
BASE = "https://www.stats.indiana.edu/sdfdata/"
COUNTY_PREFIXES = ("46", "71")
COUNTY_IDS = {"46", "71"}
COUNTY_NAMES = ("porte", "joseph")

csv.field_size_limit(10 << 20)


def norm_apn(s):
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def url_for(year):
    return f"{BASE}SDF_View_CSV_{year}.zip" if year <= 2020 else f"{BASE}SDF_{year}.zip"


def download(url, dest, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "landos-engine/1.0"})
            with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  [retry {i+1}] {e}", flush=True)
            time.sleep(5 * (i + 1))
    return False


def decode_member(zf, name):
    raw = zf.read(name)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def dict_reader(zf, name):
    lines = decode_member(zf, name).splitlines()
    if not lines:
        return None
    delim = max(("|", "\t", ","), key=lines[0].count)
    return csv.DictReader(lines, delimiter=delim)


def col(fieldnames, *needles, exclude=()):
    """First column whose lowercase name contains ALL needles and no exclude term."""
    for c in fieldnames or []:
        lc = (c or "").lower()
        if all(n in lc for n in needles) and not any(x in lc for x in exclude):
            return c
    return None


def keep_row(parcel, county_name, county_id):
    if norm_apn(parcel)[:2] in COUNTY_PREFIXES:
        return True
    if (county_id or "").strip().lstrip("0").zfill(2) in COUNTY_IDS:
        return True
    lc = (county_name or "").lower()
    return any(n in lc for n in COUNTY_NAMES)


def rows_old_era(zf, name):
    r = dict_reader(zf, name)
    fn = r.fieldnames
    parcel_cols = [c for c in fn if re.fullmatch(r"Parcel[123]", c or "")]
    c_date = col(fn, "conveyance")
    c_price = col(fn, "c6_sales_price") or col(fn, "sales_price", exclude=("assessor",))
    c_quit = col(fn, "quit")
    c_invol = col(fn, "sheriff") or col(fn, "foreclos")
    c_cid = col(fn, "county_id") or col(fn, "county")
    if not (parcel_cols and c_date):
        raise RuntimeError(f"old-era cols not found in {fn[:10]}")
    for row in r:
        for pc in parcel_cols:
            p = (row.get(pc) or "").strip()
            if not p:
                continue
            cid = row.get(c_cid, "") if c_cid else ""
            if keep_row(p, "", cid):
                yield (p, cid, row.get(c_date, ""), row.get(c_price, "") if c_price else "",
                       row.get(c_quit, "") if c_quit else "", row.get(c_invol, "") if c_invol else "")


def rows_new_era(zf, disc_name, parcel_name):
    r = dict_reader(zf, disc_name)
    fn = r.fieldnames
    c_id = col(fn, "sdf_id")
    c_date = col(fn, "conveyance")
    c_cname = col(fn, "county_name")
    c_cid = col(fn, "county_id")
    c_price = col(fn, "e1_sales_price") or col(fn, "sales_price")
    c_quit = col(fn, "quit")
    c_sher = col(fn, "sheriff")
    if not (c_id and c_date):
        raise RuntimeError(f"SALEDISC cols not found in {fn[:10]}")
    disc = {}
    for row in r:
        disc[row.get(c_id, "")] = (
            row.get(c_date, ""), row.get(c_cname, "") if c_cname else "",
            row.get(c_cid, "") if c_cid else "",
            row.get(c_price, "") if c_price else "",
            row.get(c_quit, "") if c_quit else "", row.get(c_sher, "") if c_sher else "")
    r2 = dict_reader(zf, parcel_name)
    fn2 = r2.fieldnames
    c_id2 = col(fn2, "sdf_id")
    c_parcel = col(fn2, "parcel_number", exclude=("verified",))
    if not (c_id2 and c_parcel):
        raise RuntimeError(f"SALEPARCEL cols not found in {fn2[:10]}")
    for row in r2:
        p = (row.get(c_parcel) or "").strip()
        d = disc.get(row.get(c_id2, ""))
        if not (p and d):
            continue
        date, cname, cid, price, quit_, sher = d
        if keep_row(p, cname, cid):
            yield (p, cname or cid, date, price, quit_, sher)


def main():
    out_path = sys.argv[1]
    tmp = out_path + ".tmp.zip"
    years_ok, years_failed, total = [], [], 0
    with open(out_path, "w", newline="") as out_f:
        w = csv.writer(out_f)
        w.writerow(["sdf_year", "parcel_number", "county_name", "conveyance_date",
                    "sale_price", "quitclaim", "sheriff_sale"])
        for year in YEARS:
            url = url_for(year)
            print(f"[{year}] {url}", flush=True)
            if not download(url, tmp):
                years_failed.append(year)
                print("  [FAIL] download", flush=True)
                continue
            try:
                n_year = 0
                with zipfile.ZipFile(tmp) as zf:
                    names = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
                    disc = next((n for n in names if "saledisc" in n.lower()), None)
                    parc = next((n for n in names if "saleparcel" in n.lower()), None)
                    gen = (rows_new_era(zf, disc, parc) if disc and parc
                           else rows_old_era(zf, names[0]))
                    for row in gen:
                        w.writerow((year,) + row)
                        n_year += 1
                total += n_year
                years_ok.append(year)
                print(f"  [ok] {n_year} rows (running total {total})", flush=True)
            except Exception as e:  # noqa: BLE001
                years_failed.append(year)
                print(f"  [FAIL] {e}", flush=True)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    h = hashlib.sha256()
    with open(out_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    manifest = {
        "source_pattern": [f"{BASE}SDF_View_CSV_YYYY.zip (2008-2020)", f"{BASE}SDF_YYYY.zip (2021+)"],
        "attribution": "Indiana Sales Disclosure data, DLGF via STATS Indiana (IU IBRC)",
        "counties": "18091 LaPorte (46) + 18141 St. Joseph (71)",
        "years_ok": years_ok, "years_failed": years_failed, "rows": total,
        "sha256": h.hexdigest(), "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(out_path + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[done] {total} rows, ok={years_ok}, failed={years_failed}", flush=True)


if __name__ == "__main__":
    main()
