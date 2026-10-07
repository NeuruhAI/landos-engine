"""One receipt per route run (FR-1): rows_tried / hits / new_contacts / new_heirs /
as_of — or an H-12 halt naming the human step. Receipts are JSON files first
(custody dir), then landed on the spine by the writer path when it is open."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

RECEIPT_VERSION = "free_route_receipt.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RouteReceipt:
    route_id: str                      # T1..T10, I1..I5
    route_name: str
    mode: str                          # AUTOMATED | MANUAL | BLOCKED
    source: str                        # official source name
    license: str                       # terms / license statement, quoted
    provenance_url: str
    rows_tried: int = 0
    hits: int = 0
    new_contacts: int = 0
    new_heirs: int = 0
    source_as_of: Optional[str] = None  # the date the source says it is true; None = unknown
    observed_at: str = field(default_factory=utc_now_iso)
    halted: bool = False
    halt_code: Optional[str] = None     # H-11 | H-12
    human_step: Optional[str] = None    # exact human step when halted
    worklist_path: Optional[str] = None
    counties: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    version: str = RECEIPT_VERSION

    def hit_rate(self) -> Optional[float]:
        return round(self.hits / self.rows_tried, 4) if self.rows_tried else None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["hit_rate"] = self.hit_rate()
        return d


def write_receipt(receipt: RouteReceipt, out_dir: str) -> str:
    """Write <out_dir>/<route_id>_<stamp>.receipt.json and return its path; the
    file's sha256 is echoed in a sibling SHA256SUMS line so the court can chain it."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = receipt.observed_at.replace(":", "").replace("-", "")
    slug = "".join(ch if ch.isalnum() else "-" for ch in receipt.route_name.lower()).strip("-")[:40]
    path = os.path.join(out_dir, f"{receipt.route_id}_{slug}_{stamp}.receipt.json")
    payload = json.dumps(receipt.to_dict(), indent=2, sort_keys=True, default=str)
    with open(path, "w") as f:
        f.write(payload)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    with open(os.path.join(out_dir, "SHA256SUMS"), "a") as f:
        f.write(f"{sha}  {os.path.basename(path)}\n")
    return path


def halted_receipt(route_id: str, route_name: str, *, mode: str, source: str, license: str,
                   provenance_url: str, halt_code: str, human_step: str, rows_tried: int = 0,
                   worklist_path: Optional[str] = None, counties: Optional[List[str]] = None,
                   source_as_of: Optional[str] = None) -> RouteReceipt:
    return RouteReceipt(route_id=route_id, route_name=route_name, mode=mode, source=source,
                        license=license, provenance_url=provenance_url, rows_tried=rows_tried,
                        halted=True, halt_code=halt_code, human_step=human_step,
                        worklist_path=worklist_path, counties=counties or [], source_as_of=source_as_of)
