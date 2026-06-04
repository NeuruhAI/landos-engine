"""
base.py — the common Normalizer interface for the signal-sourcing layer.

Each source is ONE module implementing Normalizer:
  fetch()      -> pull raw payload from a PUBLIC-records endpoint
  normalize()  -> map raw -> candidate signal dicts
                  {apn,address,lat,lng,signal_type,signal_strength,signal_date,raw_meta}
  run()        -> ensure the source row, upsert signals, and write a compliance
                  audit row to signal_ingest_log (source, timestamp, row_count)

Adding a new source = a new subclass, NOT new framework code (rows-not-code).
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

CandidateSignal = Dict[str, Any]  # apn,address,lat,lng,signal_type,signal_strength,signal_date,raw_meta


class Normalizer(abc.ABC):
    # subclasses set these identity fields
    source_id: str
    source_type: str
    name: str
    county: Optional[str] = None
    fetch_method: str = "csv_http"
    schedule: Optional[str] = None
    provenance: str = ""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    # ---- per-source contract -------------------------------------------------
    @abc.abstractmethod
    def fetch(self) -> Any:
        """Return the raw source payload. PUBLIC records only."""

    @abc.abstractmethod
    def normalize(self, raw: Any) -> List[CandidateSignal]:
        """Map the raw payload to candidate signal dicts."""

    # ---- shared plumbing (no per-source code) --------------------------------
    def ensure_source(self, cur) -> None:
        cur.execute(
            """
            insert into landos.signal_sources
              (source_id, source_type, name, county, fetch_method, schedule,
               provenance, enabled, tenant_id, last_run, updated_at)
            values (%s,%s,%s,%s,%s,%s,%s,true,%s, now(), now())
            on conflict (source_id) do update set
              source_type=excluded.source_type, name=excluded.name, county=excluded.county,
              fetch_method=excluded.fetch_method, schedule=excluded.schedule,
              provenance=excluded.provenance, last_run=now(), updated_at=now()
            """,
            (self.source_id, self.source_type, self.name, self.county,
             self.fetch_method, self.schedule, self.provenance, self.tenant_id),
        )

    def upsert_signals(self, cur, candidates: List[CandidateSignal]) -> int:
        n = 0
        for c in candidates:
            apn = (c.get("apn") or "").strip() or None
            cur.execute(
                """
                insert into landos.signals
                  (tenant_id, parcel_apn, address, lat, lng, signal_type,
                   signal_strength, signal_date, source_id, raw_meta, updated_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                on conflict (tenant_id, source_id, parcel_apn, signal_type) do update set
                  address=excluded.address, lat=excluded.lat, lng=excluded.lng,
                  signal_strength=excluded.signal_strength, signal_date=excluded.signal_date,
                  raw_meta=excluded.raw_meta, updated_at=now()
                """,
                (self.tenant_id, apn, c.get("address"), c.get("lat"), c.get("lng"),
                 c.get("signal_type", self.source_type),
                 c.get("signal_strength", 0.5), c.get("signal_date"),
                 self.source_id, Jsonb(c.get("raw_meta") or {})),
            )
            n += 1
        return n

    def log_ingest(self, cur, row_count: int, status: str = "ok", notes: str = "") -> None:
        cur.execute(
            "insert into landos.signal_ingest_log (tenant_id, source_id, row_count, status, notes) "
            "values (%s,%s,%s,%s,%s)",
            (self.tenant_id, self.source_id, row_count, status, notes),
        )

    def run(self, conn) -> int:
        """fetch -> normalize -> upsert -> audit-log, committed together."""
        with conn.cursor() as cur:
            self.ensure_source(cur)
            try:
                raw = self.fetch()
                candidates = self.normalize(raw)
            except Exception as e:  # noqa: BLE001 - audit the failure, then surface it
                self.log_ingest(cur, 0, "error", f"{e.__class__.__name__}: {e}")
                conn.commit()
                raise
            n = self.upsert_signals(cur, candidates)
            self.log_ingest(cur, n, "ok" if n else "empty", f"{self.name}: {n} signals")
        conn.commit()
        return n
