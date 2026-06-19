"""
import_partner_leads.py — IMPORT-1: the engine-side partner lead importer.

    python -m scripts.import_partner_leads --file data/incoming/bryan_leads.csv
    python -m scripts.import_partner_leads --sheet-url "https://docs.google.com/spreadsheets/d/<id>/..."
    python -m scripts.import_partner_leads            # poll public.lead_import_batches
    ... [--dry-run] [--provenance partner_import:bryan] [--mark-consumed]

Walks a partner (Bryan) lead file into the engine:

  intake (file | sheet-url | staging poll)
     -> fuzzy column-map (synonyms + difflib; T1's column_mapping wins when a
        staging batch carries one)
     -> dedupe vs landos.owner_leads:
          1) APN exact, format-normalized (canonical = alnum-only uppercase)
          2) owner+situs fuzzy second (county-scoped; token-sorted owner name
             SequenceMatcher >= .88 AND situs >= .80 -> MERGE; the .70-.88 band
             is HELD for operator review — a wrong merge is worse than a dup
             report, and "never duplicate" is enforced by holding, not guessing)
     -> MERGE provenance on matches (raw_meta.partner_imports list append;
        partner phones/emails into skip_trace_meta merge-never-clobber, source-
        stamped, low confidence 0.5, NO skip_trace_status change — P3 owns it)
     -> INSERT new rows with source='partner_import',
        provenance='<tag> sha256=<file sha>' (+ batch_id when staged)
     -> audit row to landos.owner_leads_import_log + manifest JSON in data/
     -> prints the scoring + sync commands per affected county (engine scores,
        then sync_to_crm.py projects qualified rows; sync never inserts CRM
        cards — the CRM owns its row set).

WRITE SURFACE: landos.owner_leads + owner_leads_import_log ONLY. The staging
tables are public.* (T1's DDL): this importer READS them; flipping their
status/consumed_at is implemented but OFF by default (--mark-consumed) until
the operator confirms the engine may write T1's mailbox columns.

Rows with no usable APN AND no usable owner+situs are REJECTED (counted, never
inserted); rows in an unconfigured county are HELD (no fabricated county_fips).
GATE IMPORT-1 = one real Bryan file walked end-to-end; nothing here mocks that.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from difflib import SequenceMatcher, get_close_matches

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.normalizers.reportall_owner import COUNTY_CONFIG
from app.run_manifest import sha256_file, git_commit_info, utc_now

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
SCRIPT_VERSION = "import_partner_leads/1.0.0"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OWNER_MERGE = 0.88   # token-sorted owner-name similarity for an auto-merge
SITUS_MERGE = 0.80   # situs similarity that must ALSO hold
HOLD_BAND = 0.70     # above this but below merge -> held for operator review

# canonical field -> header synonyms (normalized: lowercase, alnum only)
SYNONYMS = {
    "parcel_apn": ["apn", "parcelapn", "parcel", "parcelid", "parcelnumber",
                   "pin", "taxid", "taxparcel", "taxparcelid", "parcelno"],
    "owner_name": ["owner", "ownername", "name", "seller", "ownerfullname",
                   "taxpayer", "ownersname"],
    "situs_address": ["situs", "situsaddress", "propertyaddress", "property",
                      "address", "propaddress", "siteaddress", "location"],
    "owner_mailing_address": ["mailingaddress", "mailing", "owneraddress",
                              "ownermailing", "mailaddress", "mailingstreet"],
    "owner_mailing_city": ["mailingcity", "city", "ownercity"],
    "owner_mailing_state": ["mailingstate", "state", "ownerstate", "st"],
    "owner_mailing_zip": ["mailingzip", "zip", "zipcode", "ownerzip", "postalcode"],
    "county": ["county", "countyname", "situscounty"],
    "acreage": ["acreage", "acres", "lotacres", "lotsize"],
    "phone": ["phone", "phonenumber", "phone1", "ownerphone", "cell", "mobile",
              "cellphone", "cellnumber", "mobilephone", "contactphone"],
    "email": ["email", "emailaddress", "owneremail"],
    "notes": ["notes", "note", "comments", "remarks"],
}


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def map_columns(headers, t1_mapping=None):
    """header -> canonical field. T1's explicit column_mapping (canonical->header
    or header->canonical) wins; the rest fall to synonyms, then difflib."""
    mapping = {}
    if t1_mapping:
        for a, b in t1_mapping.items():
            if a in SYNONYMS:           # canonical -> header
                mapping[b] = a
            elif b in SYNONYMS:         # header -> canonical
                mapping[a] = b
    syn_to_field = {s: f for f, syns in SYNONYMS.items() for s in syns}
    for h in headers:
        if h in mapping:
            continue
        n = norm_header(h)
        if n in syn_to_field:
            mapping[h] = syn_to_field[n]
            continue
        close = get_close_matches(n, syn_to_field.keys(), n=1, cutoff=0.85)
        if close:
            mapping[h] = syn_to_field[close[0]]
    return mapping


def canon_apn(apn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (apn or "").upper())


def canon_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())).strip()


def token_sort(s: str) -> str:
    return " ".join(sorted(canon_text(s).split()))


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def county_to_fips(county: str, state: str = "") -> str | None:
    c = canon_text(county).replace(" COUNTY", "").strip()
    st = canon_text(state)
    for fips, cfg in COUNTY_CONFIG.items():
        region = cfg["region"]                       # e.g. 'Berrien County, MI'
        rname = canon_text(region.split(" County")[0])
        rst = canon_text(region.rsplit(",", 1)[-1])
        if c == rname and (not st or st == rst):
            return fips
    return None


def norm_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return None


# ---- intake modes -----------------------------------------------------------

def rows_from_file(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows, sha256_file(path), os.path.basename(path), None


def rows_from_sheet(url):
    """Google Sheet -> its CSV export; the fetched bytes are saved to
    data/incoming/ first (provenance: hash what was actually received)."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if m:
        gid = (re.search(r"[#?&]gid=(\d+)", url) or [None, "0"])[1]
        export = (f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
                  f"/export?format=csv&gid={gid}")
    else:
        export = url   # already a direct CSV link
    with urllib.request.urlopen(export, timeout=60) as resp:
        data = resp.read()
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPO, "data", "incoming", f"partner_sheet_{stamp}.csv")
    with open(path, "wb") as f:
        f.write(data)
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    return rows, sha256_file(path), export, None


def batches_from_staging(cur):
    """READ T1's mailbox: pending batches + their raw rows."""
    cur.execute(
        """select * from public.lead_import_batches
           where tenant_id=%s and consumed_at is null
             and coalesce(status,'pending') in ('pending','uploaded','ready')
           order by created_at""", (TENANT,))
    batches = cur.fetchall()
    out = []
    for b in batches:
        cur.execute(
            """select row_num, raw from public.lead_import_rows
               where batch_id=%s and consumed_at is null order by row_num""",
            (b["batch_id"],))
        rows = [r["raw"] for r in cur.fetchall()]
        out.append((rows, b["file_sha256"] or "unknown",
                    b["file_name"] or b["sheet_url"] or str(b["batch_id"]), b))
    return out


# ---- core -------------------------------------------------------------------

def process(cur, rows, file_sha, file_label, batch, provenance_tag, dry_run):
    headers = list(rows[0].keys()) if rows else []
    t1_map = (batch or {}).get("column_mapping")
    colmap = map_columns(headers, t1_map)
    fields_found = sorted(set(colmap.values()))
    print(f"\n=== {file_label} (sha {file_sha[:12]}…, {len(rows)} rows) ===")
    print(f"column map: " + ", ".join(f"{h}->{f}" for h, f in colmap.items()))
    unmapped = [h for h in headers if h not in colmap]
    if unmapped:
        print(f"unmapped columns (kept in raw_meta): {unmapped}")
    if "parcel_apn" not in fields_found and not {"owner_name", "situs_address"} <= set(fields_found):
        print("STOP — no APN column and no owner+situs pair mappable; cannot "
              "dedupe safely. Fix headers or supply column_mapping.")
        return None

    # the existing engine rows we dedupe against (qualified + seeds — never dup anything)
    cur.execute(
        """select lead_id, parcel_apn, county_fips, owner_name, situs_address,
                  skip_trace_meta, raw_meta
           from landos.owner_leads where tenant_id=%s""", (TENANT,))
    existing = cur.fetchall()
    by_apn = {}
    by_county = defaultdict(list)
    for e in existing:
        by_apn[canon_apn(e["parcel_apn"])] = e
        by_county[e["county_fips"]].append(e)

    fetched_at = utc_now().isoformat().replace("+00:00", "Z")
    prov_block_base = {
        "source": provenance_tag,
        "file": file_label,
        "file_sha256": file_sha,
        "batch_id": str(batch["batch_id"]) if batch else None,
        "fetched_at": fetched_at,
    }
    counts = defaultdict(int)
    held, rejected, merged_ids, inserted_ids = [], [], [], []
    affected_counties = set()

    for i, raw in enumerate(rows, start=1):
        rec = {f: (raw.get(h) or "").strip() for h, f in colmap.items()}
        apn_c = canon_apn(rec.get("parcel_apn", ""))
        owner = rec.get("owner_name", "")
        situs = rec.get("situs_address", "")
        if not apn_c and not (owner and situs):
            rejected.append((i, "no APN and no owner+situs"))
            counts["rejected"] += 1
            continue

        fips = county_to_fips(rec.get("county", ""), rec.get("owner_mailing_state", ""))

        # -- 1) APN exact ------------------------------------------------------
        match, matched_by = by_apn.get(apn_c), "apn_exact" if apn_c in by_apn else None

        # -- 2) owner+situs fuzzy, county-scoped -------------------------------
        if not match and owner and situs:
            pool = by_county.get(fips) if fips else existing
            best, best_o, best_s = None, 0.0, 0.0
            for e in pool or []:
                o = sim(token_sort(owner), token_sort(e["owner_name"]))
                s = sim(canon_text(situs), canon_text(e["situs_address"]))
                if o + s > best_o + best_s:
                    best, best_o, best_s = e, o, s
            if best and best_o >= OWNER_MERGE and best_s >= SITUS_MERGE:
                match, matched_by = best, f"fuzzy owner={best_o:.2f} situs={best_s:.2f}"
            elif best and ((best_o >= HOLD_BAND and best_s >= HOLD_BAND)
                           or best_s >= 0.90):
                # same situs + different owner is the resold-parcel trap:
                # neither merge (clobbers a possibly-newer owner) nor insert
                # (duplicates the parcel) is safe — operator decides.
                held.append((i, owner, situs, best["owner_name"],
                             best["situs_address"], f"o={best_o:.2f} s={best_s:.2f}"))
                counts["held_ambiguous"] += 1
                continue

        phones = [p for p in [norm_phone(rec.get("phone"))] if p]
        email = (rec.get("email") or "").strip() or None

        if match:
            counts["merged"] += 1
            merged_ids.append(str(match["lead_id"]))
            if not dry_run:
                raw_meta = match["raw_meta"] or {}
                imports = list(raw_meta.get("partner_imports") or [])
                imports.append({**prov_block_base, "row_num": i,
                                "matched_by": matched_by, "raw_row": raw})
                raw_meta = {**raw_meta, "partner_imports": imports}
                meta = match["skip_trace_meta"] or {}
                if phones or email:
                    have = {p.get("number") for p in (meta.get("phones") or [])}
                    new_phones = (meta.get("phones") or []) + [
                        {"number": p, "type": None, "confidence": 0.5,
                         "source": provenance_tag, "verified_2x": False,
                         "provenance": prov_block_base}
                        for p in phones if p not in have]
                    emails = list(dict.fromkeys((meta.get("emails") or [])
                                                + ([email] if email else [])))
                    meta = {**meta, "phones": new_phones, "emails": emails}
                cur.execute(
                    """update landos.owner_leads
                       set raw_meta=%s, skip_trace_meta=%s, updated_at=now()
                       where tenant_id=%s and lead_id=%s""",
                    (Jsonb(raw_meta), Jsonb(meta) if meta else None,
                     TENANT, match["lead_id"]))
            continue

        # -- insert new --------------------------------------------------------
        if not fips:
            held.append((i, owner, situs, None, None,
                         f"unconfigured county {rec.get('county') or '?'} — no fabricated fips"))
            counts["held_no_county"] += 1
            continue
        counts["inserted"] += 1
        affected_counties.add(fips)
        if dry_run:
            continue
        lead_id = uuid.uuid4()
        meta = None
        if phones or email:
            meta = {"phones": [{"number": p, "type": None, "confidence": 0.5,
                                "source": provenance_tag, "verified_2x": False,
                                "provenance": prov_block_base} for p in phones],
                    "emails": [email] if email else []}
        cur.execute(
            """insert into landos.owner_leads
                 (lead_id, tenant_id, county_fips, parcel_apn, situs_address,
                  owner_name, owner_mailing_address, owner_mailing_city,
                  owner_mailing_state, owner_mailing_zip, acreage,
                  source, provenance, raw_meta, skip_trace_meta)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (lead_id, TENANT, fips, rec.get("parcel_apn") or f"PARTNER-{file_sha[:8]}-{i}",
             situs or None, owner or None,
             rec.get("owner_mailing_address") or None,
             rec.get("owner_mailing_city") or None,
             rec.get("owner_mailing_state") or None,
             rec.get("owner_mailing_zip") or None,
             float(rec["acreage"]) if rec.get("acreage", "").replace(".", "", 1).isdigit() else None,
             "partner_import",
             f"{provenance_tag} sha256={file_sha}",
             Jsonb({"partner_imports": [{**prov_block_base, "row_num": i,
                                         "matched_by": "new", "raw_row": raw}]}),
             Jsonb(meta) if meta else None))
        inserted_ids.append(str(lead_id))

    # ---- report -------------------------------------------------------------
    print(f"\nrow outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for i, o, s, mo, ms, why in held:
        print(f"  HELD row {i}: {o!r} @ {s!r}" +
              (f" ~ existing {mo!r} @ {ms!r} ({why})" if mo else f" — {why}"))
    for i, why in rejected:
        print(f"  REJECTED row {i}: {why}")
    return {"counts": dict(counts), "held": len(held), "rejected": len(rejected),
            "merged_ids": merged_ids, "inserted_ids": inserted_ids,
            "affected_counties": sorted(affected_counties),
            "file": file_label, "sha256": file_sha,
            "batch_id": str(batch["batch_id"]) if batch else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file")
    src.add_argument("--sheet-url")
    ap.add_argument("--provenance", default="partner_import:bryan")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mark-consumed", action="store_true",
                    help="also stamp T1's staging rows consumed (public.* write — "
                         "OFF until the operator confirms the mailbox handshake)")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        if args.file:
            jobs = [rows_from_file(args.file)]
        elif args.sheet_url:
            jobs = [rows_from_sheet(args.sheet_url)]
        else:
            jobs = batches_from_staging(cur)
            if not jobs:
                print("staging poll: no pending batches in public.lead_import_batches — "
                      "nothing to import.")
                return 0

        results = []
        for rows, sha, label, batch in jobs:
            if not rows:
                print(f"=== {label}: 0 rows — skipped")
                continue
            r = process(cur, rows, sha, label, batch, args.provenance, args.dry_run)
            if r is None:
                conn.rollback()
                return 1
            results.append(r)
            if batch and args.mark_consumed and not args.dry_run:
                cur.execute("""update public.lead_import_batches
                               set status='consumed', consumed_at=now()
                               where batch_id=%s""", (batch["batch_id"],))
                cur.execute("""update public.lead_import_rows
                               set status='consumed', consumed_at=now()
                               where batch_id=%s""", (batch["batch_id"],))

        if args.dry_run:
            conn.rollback()
            print("\nDRY RUN — rolled back, nothing written.")
        else:
            now = utc_now()
            for r in results:
                cur.execute(
                    """insert into landos.owner_leads_import_log
                         (tenant_id, source, county_fips, row_count, status, notes)
                       values (%s,%s,%s,%s,%s,%s)""",
                    (TENANT, args.provenance,
                     "|".join(r["affected_counties"]) or "*",
                     r["counts"].get("inserted", 0) + r["counts"].get("merged", 0),
                     "ok",
                     json.dumps({k: r[k] for k in ("file", "sha256", "counts",
                                                   "held", "rejected", "batch_id")})))
            conn.commit()
            manifest = {
                "run_id": str(uuid.uuid4()),
                "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
                "kind": "import_partner_leads",
                "git_commit": git_commit_info(REPO).get("git_commit"),
                "scorer_version": SCRIPT_VERSION,
                "provenance_tag": args.provenance,
                "files": results,
            }
            mpath = os.path.join(REPO, "data",
                                 f"import_partner_{now.strftime('%Y%m%dT%H%M%SZ')}.manifest.json")
            with open(mpath, "w") as f:
                json.dump(manifest, f, indent=2)
            print(f"\nmanifest {mpath}")

        all_counties = sorted({c for r in results for c in r["affected_counties"]})
        if all_counties:
            print("\nnext (engine scores, then projection):")
            for fips in all_counties:
                alias = COUNTY_CONFIG[fips]["region"].split(" County")[0].lower().replace(". ", "")
                print(f"  python -m scripts.owner_leads --county {alias}")
            print("  python -m scripts.sync_to_crm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
