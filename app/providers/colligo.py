"""
colligo.py — tier 1c: Colligo GIS (colligogis.com, Coleman Engineering), the
county-official viewer for several UP/northern-MI counties (ENRICH-1 census
2026-07-05: Kalkaska, Houghton, Gogebic, Crawford).

NOT ArcGIS: a PHP app with a public-link session. Three interaction shapes,
selected per county by cfg["mode"]:

  findTab   — POST publink -> session cookie; POST term=<APN>&uniqType=<layer>
              to /web/int_findTabData.php -> DataTables JSON rows.
  findProps — POST publink; POST term to /web/int_findSomething.php ->
              matched ogrIDs; POST getType/ogrID/pullAmount=full to
              /web/int_loadProps.php -> per-parcel props JSON.
  bulk      — POST publink; GET /web/; POST getType=poly&getSubType=<layer>
              to /web/int_loadData.php -> ONE GeoJSON of the whole county,
              cached to disk; APNs match locally (politest possible shape:
              one request serves every cohort row).

Auth doctrine: PUBLIC link only (the publink token is the county's own
public-map URL fragment) — login-gated Colligo counties (Menominee, Keweenaw,
Ontonagon) stay UNCOVERED. Field mapping is config data (same *_field keys as
county_rest); unmapped fields stay honest-NULL. Requires Referer +
X-Requested-With headers (verified live 2026-07-05; without them the app 500s).
No geometry served -> coords stay NULL.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.parse
import urllib.request
from typing import List

from .base import ParcelProvider, ParcelDict, ProviderUnavailable
from .config import COLLIGO_ENDPOINTS

ROOT = "https://colligogis.com"
UA = "Mozilla/5.0 (compatible; landos-engine/1.0; contact jeramieshicks@gmail.com)"
BULK_CACHE_DIR = os.path.expanduser("~/models/provenance/colligo_bulk")


def _canon(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


class ColligoProvider(ParcelProvider):
    name = "colligo"
    cost_note = "free — county-official Colligo GIS public link"

    def __init__(self, tenant_id: str):
        super().__init__(tenant_id)
        self._sessions = {}     # county_fips -> opener with live session cookie
        self._bulk = {}         # county_fips -> {canon(apn): properties}

    def _cfg(self, county_fips: str) -> dict:
        cfg = COLLIGO_ENDPOINTS.get(county_fips)
        if not cfg:
            raise ProviderUnavailable(f"no Colligo config for {county_fips}")
        if not cfg.get("verified"):
            raise ProviderUnavailable(f"Colligo config for {county_fips} is UNVERIFIED")
        return cfg

    def _headers(self):
        return {"User-Agent": UA, "Referer": f"{ROOT}/web/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded"}

    def _opener(self, county_fips: str, cfg: dict):
        """One session per county: POST the public link, warm /web/."""
        if county_fips in self._sessions:
            return self._sessions[county_fips]
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            op.open(urllib.request.Request(
                f"{ROOT}/desktop/index.php",
                data=urllib.parse.urlencode({"publink": cfg["publink"]}).encode(),
                headers=self._headers()), timeout=30).read()
            op.open(urllib.request.Request(
                f"{ROOT}/web/", headers={"User-Agent": UA}), timeout=30).read()
        except Exception as e:
            raise ProviderUnavailable(f"Colligo session failed: {e}") from e
        self._sessions[county_fips] = op
        return op

    def _post(self, op, path: str, data: dict):
        try:
            req = urllib.request.Request(
                f"{ROOT}/web/{path}",
                data=urllib.parse.urlencode(data).encode(), headers=self._headers())
            with op.open(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise ProviderUnavailable(f"Colligo {path} error: {e}") from e

    # ---- the three shapes ------------------------------------------------------
    def _rows_findtab(self, op, cfg, apn) -> List[dict]:
        body = self._post(op, "int_findTabData.php",
                          {"term": apn, "uniqType": cfg["layer"]})
        try:
            d = json.loads(body)
        except ValueError as e:
            raise ProviderUnavailable(f"Colligo findTab unparseable: {e}") from e
        return [r for r in (d.get("data") or []) if isinstance(r, dict)]

    def _rows_findprops(self, op, cfg, apn) -> List[dict]:
        body = self._post(op, "int_findSomething.php", {"term": apn})
        try:
            matched = (json.loads(body).get("matched") or {}).get(cfg["layer"]) or []
        except ValueError as e:
            raise ProviderUnavailable(f"Colligo findSomething unparseable: {e}") from e
        rows = []
        for ogr_id in matched[:5]:
            body = self._post(op, "int_loadProps.php",
                              {"getType": cfg["layer"], "getSubType": "",
                               "ogrID": ogr_id, "pullAmount": "full"})
            try:
                props = json.loads(body)
            except ValueError:
                continue
            props = props.get("props") if isinstance(props.get("props"), dict) else props
            if isinstance(props, dict):
                rows.append(props)
        return rows

    def _rows_bulk(self, op, cfg, county_fips, apn) -> List[dict]:
        """One county-wide GeoJSON pull, disk-cached; local canonical match."""
        if county_fips not in self._bulk:
            os.makedirs(BULK_CACHE_DIR, exist_ok=True)
            path = os.path.join(BULK_CACHE_DIR, f"{county_fips}.json")
            if os.path.exists(path):
                with open(path) as fh:
                    gj = json.load(fh)
            else:
                body = self._post(op, "int_loadData.php",
                                  {"getType": "poly", "getSubType": cfg["layer"]})
                try:
                    gj = json.loads(body)
                except ValueError as e:
                    raise ProviderUnavailable(f"Colligo bulk unparseable: {e}") from e
                try:
                    with open(path, "w") as fh:
                        json.dump(gj, fh)
                except OSError:
                    pass   # cache is best-effort
            idx = {}
            for feat in gj.get("features", []):
                props = feat.get("properties") or {}
                k = _canon(props.get(cfg["apn_key"]))
                if k and k not in idx:
                    idx[k] = props
            self._bulk[county_fips] = idx
        props = self._bulk[county_fips].get(_canon(apn))
        return [props] if props else []

    # ---- provider interface ------------------------------------------------------
    def _to_parcel(self, county_fips: str, cfg: dict, row: dict) -> ParcelDict:
        def f(key):
            fld = cfg.get(key)
            v = row.get(fld) if fld else None
            return (str(v).strip() or None) if v is not None else None
        return {
            "county_fips": county_fips,
            "parcel_apn": (str(row.get(cfg["apn_key"]) or "").strip() or None),
            "owner_name": f("owner_field"),
            "owner_name2": f("owner2_field"),
            "owner_mailing_address": f("mail_street_field"),
            "owner_mailing_city": f("mail_city_field"),
            "owner_mailing_state": f("mail_state_field"),
            "owner_mailing_zip": f("mail_zip_field"),
            "situs_address": f("situs_street_field"),
            "situs_city": f("situs_city_field"),
            "situs_state": f("situs_state_field"),
            "situs_zip": f("situs_zip_field"),
            "situs_lat": None, "situs_lng": None,   # no geometry via these APIs
            "provenance": f"colligo:{cfg['publink']}",
            "raw_meta": {"colligo_row": row},
        }

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        cfg = self._cfg(county_fips)
        op = self._opener(county_fips, cfg)
        mode = cfg.get("mode", "findTab")
        if mode == "findTab":
            rows = self._rows_findtab(op, cfg, apn)
        elif mode == "findProps":
            rows = self._rows_findprops(op, cfg, apn)
        elif mode == "bulk":
            rows = self._rows_bulk(op, cfg, county_fips, apn)
        else:
            raise ProviderUnavailable(f"unknown Colligo mode {mode!r}")
        return [p for p in (self._to_parcel(county_fips, cfg, r) for r in rows)
                if p["parcel_apn"]]

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        raise ProviderUnavailable("Colligo owner search not configured")
