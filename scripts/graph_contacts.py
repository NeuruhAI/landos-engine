"""
graph_contacts.py — CONTACT-1 B4: NetworkX graph analysis over the contact layer.

    python -m scripts.graph_contacts [--dry-run]

Builds one graph over the qualified lead set and mines the three structures
the directive names, writing each finding into landos.discovery_signals
(idempotent upsert on the (source_type, source_ref) natural key):

  NODES   lead:<lead_id>                (parcel cards)
          human:<norm name>             (controlling humans + relatives)
          addr:<norm mailing address>   (owner mailing addresses)
  EDGES   lead-human   controls         (skip_trace_meta.controlling_human)
          lead-human   kin / heir       (landos.contact_relations)
          lead-addr    mails_to         (owner_mailing_* columns)

  graph_hub            human connected to >= 2 parcels (degree on human nodes)
  graph_heir_network   connected component containing >= 1 is_heir edge —
                       the estate cluster (deceased owner -> heirs -> parcels)
  graph_shared_mailing mailing address receiving >= 2 parcels under >= 2
                       DISTINCT owner names — the hidden-portfolio tell
                       (one address managing many nominally-unrelated owners)

A finding is a SIGNAL, not a lead (Stage-1 feed semantics, same as 007):
status='new', confidence reflects structure strength, raw_payload carries the
member lead_ids + provenance (source, generated_at, script version, git).

WRITE SURFACE: landos.discovery_signals ONLY. READ-ONLY everywhere else.
county_fips = the dominant county among member parcels.
"""
import argparse
import json
import re
import sys
from collections import Counter

import networkx as nx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.run_manifest import git_commit_info, utc_now
import os

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
SCRIPT_VERSION = "graph_contacts/1.0.0"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm_name(s: str) -> str:
    """'PETER S. COLOVOS' -> 'PETER COLOVOS' (first+last, middles dropped) —
    same merge rule the P4 hub label uses, so the two layers agree."""
    toks = [t for t in re.sub(r"[^A-Za-z ]", " ", s or "").upper().split() if len(t) > 1]
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else " ".join(toks)


def norm_addr(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", "", (s or "").upper())
    s = re.sub(r"\b(STE|SUITE|UNIT|APT|STREET|ST|AVE|AVENUE|RD|ROAD|DR|DRIVE|BLVD|LN|LANE)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    generated_at = utc_now().isoformat().replace("+00:00", "Z")
    prov = {"source": SCRIPT_VERSION, "generated_at": generated_at,
            "git_commit": git_commit_info(REPO).get("git_commit")}

    G = nx.Graph()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select lead_id, owner_name, county_fips, parcel_apn,
                      owner_mailing_address, owner_mailing_city, owner_mailing_zip,
                      skip_trace_meta->'controlling_human'->>'name' as ch_name,
                      (skip_trace_meta->>'deceased')::boolean        as deceased
               from landos.owner_leads
               where tenant_id=%s and not discovery_seed""", (TENANT,))
        leads = cur.fetchall()
        cur.execute(
            """select cr.owner_lead_id, cr.relative_name, cr.relationship,
                      cr.is_heir, cr.confidence
               from landos.contact_relations cr
               join landos.owner_leads ol on ol.lead_id = cr.owner_lead_id
               where ol.tenant_id=%s""", (TENANT,))
        relations = cur.fetchall()

        lead_info = {}
        for r in leads:
            lid = f"lead:{r['lead_id']}"
            lead_info[lid] = r
            G.add_node(lid, kind="lead")
            if r["ch_name"] and norm_name(r["ch_name"]):
                h = f"human:{norm_name(r['ch_name'])}"
                G.add_node(h, kind="human", display=r["ch_name"])
                G.add_edge(lid, h, rel="controls")
            full_addr = " ".join(p for p in [r["owner_mailing_address"],
                                             r["owner_mailing_city"],
                                             r["owner_mailing_zip"]] if p)
            na = norm_addr(full_addr)
            if na:
                a = f"addr:{na}"
                G.add_node(a, kind="addr", display=full_addr)
                G.add_edge(lid, a, rel="mails_to")
        heir_edges = 0
        for rel in relations:
            lid = f"lead:{rel['owner_lead_id']}"
            if lid not in G:
                continue
            h = f"human:{norm_name(rel['relative_name'])}"
            G.add_node(h, kind="human", display=rel["relative_name"])
            G.add_edge(lid, h, rel="heir" if rel["is_heir"] else "kin",
                       is_heir=bool(rel["is_heir"]))
            heir_edges += rel["is_heir"] or 0

        print(f"graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges "
              f"({len(leads)} leads, heir edges: {heir_edges})")

        findings = []

        # ---- 1) hubs: human nodes touching >= 2 parcels ----------------------
        for n, d in G.nodes(data=True):
            if d.get("kind") != "human":
                continue
            parcels = [m for m in G.neighbors(n) if m.startswith("lead:")]
            if len(parcels) >= 2:
                counties = Counter(lead_info[p]["county_fips"] for p in parcels)
                findings.append({
                    "source_type": "graph_hub",
                    "source_ref": n.removeprefix("human:"),
                    "county_fips": counties.most_common(1)[0][0],
                    "owner_name": d.get("display"),
                    "confidence": min(0.95, 0.5 + 0.1 * len(parcels)),
                    "raw_payload": {**prov, "parcel_count": len(parcels),
                                    "lead_ids": [p.removeprefix("lead:") for p in parcels],
                                    "relations": sorted({G.edges[n, p].get("rel")
                                                         for p in parcels})},
                })

        # ---- 2) heir networks: components carrying an is_heir edge -----------
        for comp in nx.connected_components(G):
            sub = G.subgraph(comp)
            if not any(d.get("is_heir") for _, _, d in sub.edges(data=True)):
                continue
            parcels = [n for n in comp if n.startswith("lead:")]
            humans = [sub.nodes[n].get("display") for n in comp if n.startswith("human:")]
            anchor = min(p.removeprefix("lead:") for p in parcels)
            counties = Counter(lead_info[p]["county_fips"] for p in parcels)
            findings.append({
                "source_type": "graph_heir_network",
                "source_ref": f"estate-{anchor}",
                "county_fips": counties.most_common(1)[0][0],
                "owner_name": lead_info[f"lead:{anchor}"]["owner_name"],
                "confidence": 0.7,
                "raw_payload": {**prov, "parcel_count": len(parcels),
                                "lead_ids": [p.removeprefix("lead:") for p in parcels],
                                "humans": humans,
                                "deceased_owner": bool(
                                    lead_info[f"lead:{anchor}"]["deceased"])},
            })

        # ---- 3) shared mailing: addr nodes, >=2 parcels, >=2 owner names -----
        for n, d in G.nodes(data=True):
            if d.get("kind") != "addr":
                continue
            parcels = [m for m in G.neighbors(n) if m.startswith("lead:")]
            owners = {norm_name(lead_info[p]["owner_name"]) or
                      lead_info[p]["owner_name"] for p in parcels}
            if len(parcels) >= 2 and len(owners) >= 2:
                counties = Counter(lead_info[p]["county_fips"] for p in parcels)
                findings.append({
                    "source_type": "graph_shared_mailing",
                    "source_ref": n.removeprefix("addr:"),
                    "county_fips": counties.most_common(1)[0][0],
                    "owner_name": None,
                    "confidence": min(0.9, 0.4 + 0.1 * len(owners)),
                    "raw_payload": {**prov, "address": d.get("display"),
                                    "parcel_count": len(parcels),
                                    "distinct_owner_names": sorted(owners),
                                    "lead_ids": [p.removeprefix("lead:") for p in parcels]},
                })

        # ---- report + upsert --------------------------------------------------
        by_type = Counter(f["source_type"] for f in findings)
        print(f"findings: " + (", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
                               or "none"))
        for f in findings:
            label = f["raw_payload"].get("address") or f["source_ref"]
            print(f"  {f['source_type']:<22} {label[:44]:<44} "
                  f"parcels={f['raw_payload']['parcel_count']} conf={f['confidence']:.2f}")
        if args.dry_run:
            print("DRY RUN — nothing written.")
            return 0

        written = 0
        for f in findings:
            cur.execute(
                """insert into landos.discovery_signals
                     (tenant_id, source_type, source_ref, county_fips, owner_name,
                      confidence, raw_payload, status)
                   values (%s,%s,%s,%s,%s,%s,%s,'new')
                   on conflict (source_type, source_ref) do update
                     set raw_payload=excluded.raw_payload,
                         confidence=excluded.confidence,
                         county_fips=excluded.county_fips""",
                (TENANT, f["source_type"], f["source_ref"], f["county_fips"],
                 f["owner_name"], f["confidence"], Jsonb(f["raw_payload"])))
            written += 1
        conn.commit()
        print(f"upserted {written} discovery_signals rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
