"""
export_skip_targets.py — SKIP-PREP: the deduped human target list for the
skip-trace run (the only gap left to a callable list is phones on the human
layer).

    python -m scripts.export_skip_targets

ONE HUMAN PER ROW, hub-collapsed: a controlling human who appears on N parcels
is ONE lookup, not N (Peter Colovos's 13 BLUE STAR parcels = 1 target row that
unlocks all 13). Two populations, collapsed together so a person who is both an
individual owner AND an entity controller is a single row:

  R2 — pierced entity-controlling humans (skip_trace_meta.controlling_human.name)
  R1 — individual owners (owner_type='individual')

READ-ONLY. Emits exports/skip_targets_<YYYY-MM-DD>.csv (gitignored). Columns:

  target_name, role (owner|controller|owner+controller), recipe (R1|R2|R1+R2),
  linked_parcel_count, linked_lead_ids (|-joined), best_mailing_address,
  origin_state, source_owner_names (|-joined, the raw tax-roll/registry names)

The skip-trace vendor or VA works one row, returns phone(s); those flow back
through scripts/import_resolved.py -> skip_trace_meta -> P4 sync (DNC-gated).
"""
import csv
import os
import re
from collections import defaultdict
from datetime import date

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
QUALIFIED = "tenant_id=%s and not discovery_seed"


def canon_key(name: str):
    """Order-independent dedup key: the set of >1-char alpha tokens. Collapses
    'PETER S COLOVOS' / 'Colovos, Peter' and 'PERSON CANTRICE' / 'CANTRICE PERSON'
    to the same human regardless of last-first vs first-last source ordering."""
    toks = tuple(sorted(t for t in re.sub(r"[^A-Za-z ]", " ", (name or "").upper()).split()
                        if len(t) > 1))
    return toks


def display(name: str) -> str:
    return " ".join(w.capitalize() for w in (name or "").replace(",", " ").split())


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            f"""select lead_id, owner_name, owner_type, owner_mailing_address,
                       owner_mailing_city, owner_mailing_state, owner_mailing_zip,
                       skip_trace_meta->>'outreach_recipe'             as recipe,
                       skip_trace_meta->'controlling_human'->>'name'   as ch_name,
                       skip_trace_meta->'entity_record'->>'principal_addr'  as ent_addr,
                       skip_trace_meta->'entity_record'->>'jurisdiction'    as ent_juris
                from landos.owner_leads where {QUALIFIED}""", (TENANT,))
        leads = cur.fetchall()

    # target_key -> aggregated row
    targets = {}
    skipped_unnamed = 0

    def mail(r):
        bits = [r["owner_mailing_address"]]
        csz = " ".join(p for p in [r["owner_mailing_city"], r["owner_mailing_state"],
                                   r["owner_mailing_zip"]] if p)
        if csz:
            bits.append(csz)
        return ", ".join(b for b in bits if b)

    for r in leads:
        recipe = r["recipe"]
        if recipe == "R2" and r["ch_name"]:
            name, role = r["ch_name"], "controller"
            best_addr = r["ent_addr"] or mail(r)
            origin = r["ent_juris"] or r["owner_mailing_state"]
        elif r["owner_type"] == "individual":
            name, role = r["owner_name"], "owner"
            best_addr = mail(r)
            origin = r["owner_mailing_state"]
        else:
            # R2 agent-only (no human pierced yet) or govt/trust — not a skip target
            if recipe == "R2" and not r["ch_name"]:
                skipped_unnamed += 1
            continue

        # recipe defaults by population when meta carries no stamp (most
        # individual owners are unstamped -> R1 direct; controllers -> R2).
        # A stamped recipe (R4 estate, etc.) is preserved.
        recipe = recipe or ("R1" if role == "owner" else "R2")

        k = canon_key(name)
        if not k:
            continue
        t = targets.setdefault(k, {
            "target_name": display(name), "roles": set(), "recipes": set(),
            "lead_ids": [], "addrs": [], "origins": set(), "source_names": set(),
        })
        t["roles"].add(role)
        t["recipes"].add(recipe)
        t["lead_ids"].append(str(r["lead_id"]))
        if best_addr:
            t["addrs"].append(best_addr)
        if origin:
            t["origins"].add(origin)
        t["source_names"].add((r["owner_name"] or "").strip())

    # ---- assemble rows --------------------------------------------------------
    rows = []
    for t in targets.values():
        role = "owner+controller" if t["roles"] == {"owner", "controller"} \
            else next(iter(t["roles"]))
        recipe = "+".join(sorted(t["recipes"]))
        # best mailing address = most frequent non-empty
        addr = max(set(t["addrs"]), key=t["addrs"].count) if t["addrs"] else ""
        rows.append({
            "target_name": t["target_name"],
            "role": role,
            "recipe": recipe,
            "linked_parcel_count": len(t["lead_ids"]),
            "linked_lead_ids": "|".join(t["lead_ids"]),
            "best_mailing_address": addr,
            "origin_state": "|".join(sorted(t["origins"])),
            "source_owner_names": "|".join(sorted(n for n in t["source_names"] if n)),
        })
    rows.sort(key=lambda r: (-r["linked_parcel_count"], r["target_name"]))

    os.makedirs("exports", exist_ok=True)
    out = f"exports/skip_targets_{date.today().isoformat()}.csv"
    cols = ["target_name", "role", "recipe", "linked_parcel_count",
            "linked_lead_ids", "best_mailing_address", "origin_state",
            "source_owner_names"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # ---- report ---------------------------------------------------------------
    parcels = sum(r["linked_parcel_count"] for r in rows)
    hubs = [r for r in rows if r["linked_parcel_count"] > 1]
    collapsed = parcels - len(rows)   # lookups saved by hub-collapse
    print(f"wrote {out}")
    print(f"unique human targets: {len(rows)}")
    print(f"parcels covered:      {parcels}")
    print(f"hub-collapsed:        {len(hubs)} humans control >1 parcel; "
          f"{collapsed} lookups saved (parcels {parcels} -> {len(rows)} rows)")
    print(f"  by role: " + ", ".join(
        f"{role}={sum(1 for r in rows if r['role']==role)}"
        for role in sorted({r['role'] for r in rows})))
    print(f"R2 agent-only rows excluded (no human pierced yet): {skipped_unnamed}")
    print("\ntop hubs:")
    for r in hubs[:6]:
        print(f"  {r['target_name']:<26} {r['role']:<16} "
              f"{r['linked_parcel_count']} parcels  origin={r['origin_state']}")


if __name__ == "__main__":
    main()
