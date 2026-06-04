# LandOS Engine — multi-county land-acquisition lead intelligence.

LandOS Engine ingests parcel ownership and distress signals, scores owners on
independent axes, and produces ranked, provenance-stamped lead lists. It is the
Neuruh-owned scoring engine for the Anchorpoint JV — a standalone, API/CLI-driven
service that holds the model logic; downstream consumers receive scored lead lists
rather than the scoring internals.

## Status

**Live (real data):**
- Absentee + distressed scoring on real parcel data
- ReportAll owner pipeline (parcel ownership + mailing → `owner_leads`)
- Ranked lead CSV + outreach (mail-merge) export
- Tamper-evident run manifests + append-only `owner_leads_run_log`
- De-compressed ranking (scorer **v1.1.0**)
- **105 tests passing**

**Gated (built, OFF by default):**
- Tracerfy skip-trace ($/lead — never called unless explicitly enabled)
- APG push adapter (destination table unconfirmed; cannot write)

**Planned (framework / stubs only — NOT live):**
- Motivated axis (probate/death, aging owner, business-sale, divorce, etc.) — PLANNED,
  not yet built. No motivated package or columns exist. A parcel-keyed probate signal
  would ride the existing distress signal stack (the scorer already recognizes a
  `probate` signal type), so a v1 needs no scorer-math change — see
  docs/probate_feasibility.md.
- Forward-value / deal-quadrant layer

## Architecture

**Three lead axes** (independent — a parcel can score on any combination):
- **Absentee** — owner *location* (owner mailing address vs. situs). Live.
- **Distressed** — parcel *status* (tax-delinquent, brownfield, environmental). Live,
  read from the existing `landos.signals` stack.
- **Motivated** — owner *life-circumstance* (probate, aging, business-sale, …).
  Planned, not built — there is no `app/motivated/` package or `motivated_*` column
  yet. A probate signal would stack through the existing distress fusion.

**Multi-county by design.** County is a stored value (`county_fips`) driven by
`COUNTY_CONFIG`; adding a county is a config row (region string + APN-prefix rule),
not new code.

**Schema** (`landos` Postgres schema): `owner_leads` (the owner spine + scores),
`signals` (distress stack), `overlay_polygons` (opportunity-zone and other policy
overlays), `owner_leads_run_log` (append-only run audit), `owner_leads_import_log`,
plus the existing scoring tables. Row-level security is enabled on these tables.

**Provenance model.** Every ranked-list run writes a JSON manifest next to the CSV
and an append-only `owner_leads_run_log` row: run id, UTC timestamp, county,
git commit, scorer version, the fusion weights in effect, row counts, and a
**SHA-256 of the output CSV bytes**. Any produced list is therefore reproducible and
tamper-evident.

**Firewall.** Scoring lives here. The engine never writes to a partner CRM directly —
only the gated, swappable push adapter (`app/push/`) does, and it is OFF by default.

## Setup

Prerequisites: Python 3.11+ and a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in real values — NEVER commit .env
```

Environment variables (names only — see `.env.example`; do not commit real values):

- `DATABASE_URL` — Postgres connection (Neuruh Supabase, `landos` schema)
- `REPORTALL_CLIENT_KEY` — ReportAll Parcel Data API client key
- `APG_DATABASE_URL` *(optional)* — only needed if/when the gated APG push is enabled
- `TRACERFY_API_KEY` *(optional)* — only needed if/when the gated skip-trace is enabled

## Usage

The pipeline, in order (`--county` drives `county_fips`; Berrien shown as the example):

```bash
# 1. Pull parcel ownership from ReportAll into owner_leads
python -m scripts.ingest_owner_leads --county berrien --limit 150

# 2. Score (absentee + distressed), rank, and write data/owner_leads_<county>.csv
python -m scripts.owner_leads --county berrien

# 3. Build the mail-merge / outreach export (Tracerfy skip-trace is GATED OFF;
#    pass --skip-trace to enable it — incurs per-lead cost)
python -m scripts.export_outreach --county berrien
```

Each `owner_leads` run prints the ranked-CSV path, the run manifest path, and the
output CSV's SHA-256, and appends a row to `owner_leads_run_log`.

## Tests

```bash
python -m pytest
```

105 tests passing.

## Data handling / privacy

This engine processes **real owner PII** (names, mailing addresses). Output CSVs
(`data/*.csv`), run manifests (`data/*.manifest.json`), and `.env` are gitignored and
**must never be committed**. Downstream use of any exported lead data must respect
applicable data-protection and privacy obligations.
