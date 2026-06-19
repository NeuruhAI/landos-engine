# DISCOVERY ADAPTERS — the signal-driven discovery layer (DISCOVERY-1)

Top-of-funnel doctrine: **ingest motivated-seller SIGNALS, not surname lists.**
A signal (probate filing, obituary, estate sale, permit, tax delinquency, code
violation) becomes a candidate lead **only after it resolves to a parcel**.
Resolved signals feed `landos.owner_leads` pre-tagged with the recipe they
imply, so the existing score → classify → pierce → resolve stages run on a
MOTIVATED seed instead of a cold list.

Every source is one adapter. A new source is a new adapter module — never a
framework rewrite (same doctrine as `app/normalizers` and the per-state pierce
adapters).

## 1 — The contract (`app/discovery/base.py`)

```python
class DiscoveryAdapter(abc.ABC):
    source_type: str    # 'probate_in' — also the provenance prefix
    county_fips: str
    access_mode: str    # 'automated' | 'va_fed' | 'bulk_dua'
    access_path: str    # the exact public access point
    recipe_hint: str    # outreach recipe the signal implies (probate -> R4)

    def fetch(self) -> List[DiscoverySignal]: ...
```

`fetch()` returns normalized signal dicts:
`{source_ref, source_url, owner_name, signal_date, confidence, raw_payload,
parcel_apn?}` — `source_ref` is the source's natural key (case number, obit
URL slug) and is the idempotency anchor (`uq_discovery_signal_natural`).
A `va_fed` adapter reads the VA's hand-back file instead of the network; the
pipeline cannot tell the difference, so upgrading a source to automation never
touches the pipeline.

**Adapters produce signals. Matching and lead creation live in the pipeline**
(`scripts/ingest_discovery.py`) so match logic improves in one place for every
source.

Storage: `landos.discovery_signals` (migration 007) — lifecycle
`new → matched | no_match | va_pending | rejected`, with `matched_lead_id`
closing the signal→lead loop for match-rate measurement.

## 2 — Pipeline invariants (read before writing any adapter)

1. **Canonical APN only.** The parcel match key is the COUNTY tax APN.
   For IN (18141) that is the **71-form** carried in ReportAll `alt_id_1` —
   **NEVER the 018-form** ReportAll-internal id (that goes only in
   `owner_leads.reportall_parcel_id`). Learned in the pierce batch; encoded in
   `app/normalizers/reportall_owner.py::normalize` (`id_mode=reportall_native`).
2. **Provenance rule (the migration-004 trap).** Discovery-created leads carry
   `source='discovery'`, `provenance='discovery:<source_type>:<source_ref>'`
   — never `reportall:%`, because migration 004 marks 18141 `reportall:%` rows
   as `discovery_seed` bootstrap noise and the live queue would silently
   exclude the new motivated lead. (Naming collision note: the OLD
   `discovery_seed` column = surname-bootstrap cohort, unrelated to this layer.)
3. **Known limitation — prefix-paginated owner-name search.** The single-key
   match (decedent name → tax roll) runs ONE owner-search page (rpp=25).
   Common surnames can overflow that page and a true parcel can be silently
   missed; ReportAll's owner search is also prefix/term-based, so name-form
   drift ("YODER MARILYN R TRUST") can miss. Two-key signals (legal
   description / inventory APN) bypass this entirely — which is why IN probate
   (Ind. Code 29-1-12-1: real property classified "with plat or survey
   description") is the design-cleanest source.
4. **Firewall.** Discovery writes `landos.*` only (`discovery_signals`,
   `owner_leads`, `contact_relations`). The CRM sees discovery leads through
   the P4 sync, like every lead.
5. **ToS-clean only.** Probe robots.txt + Terms before wiring any fetch. If a
   source forbids automation, the adapter ships `va_fed` — do NOT force it.

## 3 — Adapter: `probate_in` (St. Joseph County IN, 18141) — **va_fed**

- **Access path:** https://public.courts.in.gov/mycase — Odyssey public
  docket. Search needs a party name or case number (no public
  browse-recent-filings; that's the anti-bulk design). No CAPTCHA observed
  (probed 2026-06-10). Case detail exposes: case number/type/status, decedent,
  petitioner (personal representative), the CCS filing list — and on
  unsupervised estates the **consent filings name the heir chain**.
- **Why va_fed (verbatim ToS, in.gov/courts/policies/tou-mycase):** no
  "spiders, robots, avatars, intelligent agents, or any other extraction or
  navigation search except for a normal browser"; copying limited to "an
  ordinary single use"; robots.txt additionally disallows the MyCase app paths
  and all PDFs. A human in a normal browser is compliant; automation is not.
- **The real automation path:** Indiana Office of Court Services **bulk
  data / Data-Use Agreement** (odyssey bulk extracts). That is the E2-era
  upgrade: same adapter `source_type`, `access_mode='bulk_dua'`, same signal
  shape — zero pipeline change.
- **Documents:** filing PDFs (incl. the §29-1-12-1 inventory with the legal
  description) are not downloadable from MyCase; the VA/clerk pulls them.
  Docket-only rows are still useful (name + heirs + dates = single-key match
  at 0.6 confidence); inventory rows earn two-key confidence (0.85).
- **VA workflow:** work obituaries/legal notices aged 3+ weeks (probed
  timing: obit → estate filing lags weeks — two June-2026 decedents had no
  case yet on 2026-06-10), search MyCase by decedent name with the Probate
  filter + St. Joseph County, open the case, record one CSV row per estate in
  `data/incoming/probate_in_18141.csv`:

  `case_number, case_title, court, case_type, filed_date, status,
  decedent_name, personal_rep, heirs (pipe-separated), attorney, source_url,
  legal_description, parcel_apn (71-form only)`

- **Run:** `python -m scripts.ingest_discovery --adapter probate_in`

## 4 — Adding the next adapter (obituary, estate sale, permit, …)

1. New module `app/discovery/<source>.py` subclassing `DiscoveryAdapter`;
   set `source_type/county_fips/access_mode/access_path/recipe_hint`.
2. Probe robots + ToS; record the verdict in the module docstring (like
   `probate_in.py`).
3. Emit signals with a stable `source_ref`; put everything else in
   `raw_payload`.
4. Register it in `scripts/ingest_discovery.py::ADAPTERS`.
5. If the source carries its own parcel key, populate `parcel_apn`
   (canonical county form only) for two-key confidence.

Timing/lag, silent-miss, and access findings belong HERE so the next adapter
inherits them instead of rediscovering them.
