"""
va_lookup_sheet.py — CONTACT-1 B2: the VA's TPS/FPS link worksheet.

    python -m scripts.va_lookup_sheet [--in exports/skip_targets_<date>.csv]

Takes the canonical skip-target list (exports/skip_targets_*.csv, one row per
deduped human, hub-collapsed) and emits the worksheet the VA actually works:
every row carries the owner/controller name + best mailing address + PREFILLED
TruePeopleSearch and FastPeopleSearch search URLs + the SOP-1 result columns
(named EXACTLY as scripts/import_resolved.py parses them, so worked rows flow
straight back through the P3 importer).

ZERO SCRAPING, ZERO NETWORK: this script only BUILDS urls — it never requests
them. The VA, a human in a browser, opens the links (VA_RUNBOOK SOP-1 step 1 —
that is exactly why this step is theirs; no ToS exposure on our side).

Name order: tax-roll owner names are SURNAME FIRST MIDDLE, so owner-role rows
get the runbook flip ("PERSON CANTRICE T" -> "Cantrice T Person") before URL
building; registry-pierced controller names are already First Last and pass
through. Person-pairs ("A & B") search the first person; the pair is preserved
in name_as_listed. The flip is a best guess — the VA adjusts in-browser when a
search misses (va_notes records it).

READ-ONLY on the engine: no DB writes. Output exports/va_lookup_sheet_<date>.csv
+ a manifest JSON next to it (input/output SHA-256, git commit, row counts).
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys
import urllib.parse
import uuid

from app.run_manifest import sha256_file, git_commit_info, utc_now

SCRIPT_VERSION = "va_lookup_sheet/1.0.0"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Result columns named exactly as scripts/import_resolved.py reads them.
RESULT_COLS = [
    "phone_1", "phone_1_type", "phone_1_confidence", "phone_1_source",
    "phone_2", "phone_2_type", "phone_2_confidence", "phone_2_source",
    "email_1",
    "relative_1_name", "relative_1_phone",
    "relative_2_name", "relative_2_phone",
    "verified_via", "resolved_at", "va_notes", "dnc_status",
    "outreach_recipe", "primary_target",
]

CSZ_RE = re.compile(r"^(?P<city>.+?)[,\s]+(?P<st>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")

# An owner-role name carrying any of these is an ORGANIZATION the upstream
# owner_type classifier missed, not a person: people-search is the wrong tool
# (route SOP-2 pierce via deeds/registry) and the surname flip would mangle it.
ORG_RE = re.compile(
    r"\b(LLC|INC|CORP|CORPORATION|TRUST|HOSPITAL|HOSPITALS|CHURCH|CITY|COUNTY|"
    r"STATE|TOWNSHIP|BANK|AUTHORITY|ASSOCIATION|FOUNDATION|MINISTRIES|HOLDINGS|"
    r"PROPERTIES|INVESTMENTS|PARTNERS|COMPANY|HOPE\s+VI|HABITAT|DEVELOPMENT|"
    r"COMMUNITY|HOMES|REALTY|ENTERPRISES)\b", re.IGNORECASE)


def parse_city_state_zip(addr: str):
    """'330 W. MAIN ST, SUITE 500, BENTON HARBOR, MI 49022' ->
    ('BENTON HARBOR','MI','49022'). Returns ('','','') when unparseable."""
    parts = [p.strip() for p in (addr or "").split(",") if p.strip()]
    if not parts:
        return "", "", ""
    tail = parts[-1]
    m = CSZ_RE.match(tail)
    if m:
        return m.group("city"), m.group("st"), m.group("zip")
    # state+zip alone in the last segment, city in the one before
    m = re.match(r"^(?P<st>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$", tail)
    if m and len(parts) >= 2:
        return parts[-2], m.group("st"), m.group("zip")
    return "", "", ""


def search_name(target_name: str, role: str) -> str:
    """The name to type into a people-search box. Owner-role rows arrive
    SURNAME-first from the tax roll -> flip; controllers are First Last."""
    name = (target_name or "").split("&")[0].strip()   # person-pair: first person
    toks = name.split()
    if role.startswith("owner") and "controller" not in role and len(toks) >= 2:
        toks = toks[1:] + [toks[0]]                     # surname-first -> first ... surname
    return " ".join(toks)


def tps_url(name: str, city: str, st: str, zipc: str) -> str:
    csz = " ".join(p for p in (city, st, zipc) if p)
    q = urllib.parse.urlencode({"name": name, "citystatezip": csz})
    return f"https://www.truepeoplesearch.com/results?{q}"


def fps_url(name: str, city: str, st: str) -> str:
    toks = [t for t in re.sub(r"[^A-Za-z ]", " ", name).split() if t]
    slug = "-".join((toks[0], toks[-1])).lower() if len(toks) >= 2 \
        else "-".join(toks).lower()
    if city and st:
        loc = "-".join(re.sub(r"[^A-Za-z ]", " ", city).lower().split() + [st.lower()])
        return f"https://www.fastpeoplesearch.com/name/{slug}_{loc}"
    return f"https://www.fastpeoplesearch.com/name/{slug}"


def latest_skip_targets() -> str:
    files = sorted(glob.glob(os.path.join(REPO, "exports", "skip_targets_*.csv")))
    if not files:
        sys.exit("STOP — no exports/skip_targets_*.csv found; run "
                 "scripts/export_skip_targets.py first.")
    return files[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default=None,
                    help="skip-targets CSV (default: latest exports/skip_targets_*.csv)")
    args = ap.parse_args()
    infile = args.infile or latest_skip_targets()

    with open(infile, newline="") as f:
        targets = list(csv.DictReader(f))
    if not targets:
        sys.exit(f"STOP — {infile} has no rows.")

    out_rows = []
    unparsed_addr = 0
    org_suspect = []
    for t in targets:
        city, st, zipc = parse_city_state_zip(t["best_mailing_address"])
        if not st:
            unparsed_addr += 1
        is_org = ("owner" in t["role"] and "controller" not in t["role"]
                  and bool(ORG_RE.search(t["target_name"])))
        if is_org:
            org_suspect.append(t["target_name"])
            name = t["target_name"]          # no flip — it isn't SURNAME-first
        else:
            name = search_name(t["target_name"], t["role"])
        row = {
            "target_name": t["target_name"],
            "search_name": name,
            "name_shape": "org_suspect" if is_org else "person",
            "role": t["role"],
            "recipe_hint": t["recipe"],
            # people-search on a named human (SOP-2 ends here too); an org-shaped
            # name was never pierced to a human -> deed/registry path instead
            "sop_path": "SOP-2-pierce" if is_org else "SOP-1-direct",
            "linked_parcel_count": t["linked_parcel_count"],
            "linked_lead_ids": t["linked_lead_ids"],
            "best_mailing_address": t["best_mailing_address"],
            "origin_state": t["origin_state"],
            "name_as_listed": t["source_owner_names"],
            "tps_url": tps_url(name, city, st, zipc),
            "fps_url": fps_url(name, city, st),
            **{c: "" for c in RESULT_COLS},
        }
        out_rows.append(row)

    now = utc_now()
    out = os.path.join(REPO, "exports", f"va_lookup_sheet_{now.strftime('%Y-%m-%d')}.csv")
    cols = list(out_rows[0].keys())
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    manifest = {
        "run_id": str(uuid.uuid4()),
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "kind": "va_lookup_sheet",
        "git_commit": git_commit_info(REPO).get("git_commit"),
        "scorer_version": SCRIPT_VERSION,
        "input_file": os.path.basename(infile),
        "input_sha256": sha256_file(infile),
        "row_count": len(out_rows),
        "parcels_covered": sum(int(r["linked_parcel_count"]) for r in out_rows),
        "unparsed_mailing_addresses": unparsed_addr,
        "org_suspect_owner_rows": org_suspect,
        "csv_file": os.path.basename(out),
        "csv_sha256": sha256_file(out),
        "network_calls": 0,   # link generation only — zero scraping by construction
    }
    mpath = out.replace(".csv", ".manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"wrote {out}  ({len(out_rows)} targets, "
          f"{manifest['parcels_covered']} parcels covered)")
    print(f"manifest {mpath}")
    print(f"csv sha256 {manifest['csv_sha256']}")
    if unparsed_addr:
        print(f"NOTE: {unparsed_addr} rows had unparseable mailing city/state — "
              f"their URLs are name-only; VA narrows in-browser.")
    if org_suspect:
        print(f"NOTE: {len(org_suspect)} owner rows are org-shaped names the "
              f"classifier missed -> routed sop_path=SOP-2-pierce, no name flip: "
              + "; ".join(org_suspect))
    print("zero network calls made — links generated, never requested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
