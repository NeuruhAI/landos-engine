"""T1: NC eCourts estate files. The PDFs are a human download (H-12); once dropped in
~/.neuruh/private/deedsonar/_raw/estate/pdf/<county>/ the Phase 7 parser
(app.contact.estate_pdf_intake) turns them into importer rows with a receipt per PDF."""
from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

from .attempts import Attempt
from .estate_rows import is_probate
from .human_routes import t1_estate_search, write_worklist
from .receipts import RouteReceipt, halted_receipt

DROP_ROOT = os.path.expanduser("~/.neuruh/private/deedsonar/_raw/estate/pdf")
TERMS = ("Public case information reachable in an ordinary browser; scripted clients receive HTTP 403 and Smart Search sits "
         "behind a human-verification challenge (never defeated).")


def county_drop_dir(county: str) -> str:
    return os.path.join(DROP_ROOT, county.lower())


def pdfs_for(county: str) -> List[str]:
    return sorted(glob.glob(os.path.join(county_drop_dir(county), "*.pdf")) + glob.glob(os.path.join(county_drop_dir(county), "*.PDF")))


def worklist_rows(estate_rows: List[dict], county: str, cap: int = 40) -> List[dict]:
    rows = [r for r in estate_rows if (r.get("county") or "") == county and is_probate(r) and (r.get("priority_tier") or "").upper() in ("A", "TIER_A")]
    rows.sort(key=lambda r: int(r.get("global_rank") or 10**9))
    out = []
    for r in rows[:cap]:
        s = t1_estate_search(r.get("owner_name", ""), county)
        out.append({"route_id": "T1", "global_rank": r.get("global_rank", ""), "candidate_id": r["candidate_id"], "name": r.get("owner_name", ""), "role": "decedent",
                    "county": county, "situs_address": r.get("property_address_raw", ""), "apn": r.get("apn", ""), "url": s["url"],
                    "instructions": f"Smart Search \"{s['smart_search']}\", case type Estate, county {county}; download {s['download']} into {county_drop_dir(county)}/",
                    "paste_back": "the PDFs (parsed on the next lane run)"})
    return out


def run(estate_rows: List[dict], counties: List[str], *, run_id: str, now_iso: str, worklists_dir: str, receipt_path: Optional[str]) -> Dict:
    """Per county: PDFs present → parse (the Phase 7 intake) and receipt; none → H-12 halt + worklist."""
    receipts: List[RouteReceipt] = []; attempts: List[Attempt] = []; worklists: Dict[str, str] = {}
    for county in counties:
        pdfs = pdfs_for(county)
        rows = worklist_rows(estate_rows, county)
        if not pdfs:
            path = os.path.join(worklists_dir, f"ESTATE_WORKLIST_{county.upper()}_{now_iso[:10].replace('-', '')}.csv")
            n = write_worklist(path, rows); worklists[county] = path
            receipts.append(halted_receipt("T1", f"NC eCourts estate files — {county}", mode="MANUAL", source="NC eCourts Portal (Odyssey) Smart Search",
                                           license=TERMS, provenance_url="https://portal-nc.tylertech.cloud/Portal/", halt_code="H-12",
                                           human_step=f"Download each decedent's AOC-E-201 / AOC-E-505 PDF from the Portal into {county_drop_dir(county)}/ ({n} rows in {path}); the next lane run parses them with a receipt per PDF.",
                                           rows_tried=n, worklist_path=path, counties=[county]))
            for r in rows:
                attempts.append(Attempt(r["candidate_id"], "T1", run_id, "HALTED", "NC_ECOURTS_PORTAL", None, now_iso, receipt_path, {"human_step": "download PDF", "worklist": os.path.basename(path)}))
            continue
        try:
            from app.contact.estate_pdf_intake import intake_estate_pdfs  # Phase 7 parser
            result = intake_estate_pdfs(county_drop_dir(county), county=county)  # type: ignore[call-arg]
            parsed = result.get("rows_written", 0) if isinstance(result, dict) else 0
            receipts.append(RouteReceipt("T1", f"NC eCourts estate files — {county}", "MANUAL", "AOC-E-201 / AOC-E-505 PDFs dropped by the operator", TERMS,
                                         "https://portal-nc.tylertech.cloud/Portal/", rows_tried=len(pdfs), hits=parsed, new_contacts=0, new_heirs=parsed,
                                         source_as_of=None, observed_at=now_iso, counties=[county], evidence={"pdfs": [os.path.basename(p) for p in pdfs]}))
        except Exception as e:  # noqa: BLE001 — the receipt names the failure
            receipts.append(RouteReceipt("T1", f"NC eCourts estate files — {county}", "MANUAL", "AOC PDFs", TERMS, "https://portal-nc.tylertech.cloud/Portal/",
                                         rows_tried=len(pdfs), hits=0, new_contacts=0, new_heirs=0, source_as_of=None, observed_at=now_iso, counties=[county],
                                         notes=[f"intake error: {str(e)[:200]}"]))
    return {"receipts": receipts, "attempts": attempts, "worklists": worklists}
