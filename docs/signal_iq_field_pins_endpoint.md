# `/v1/field-pins` — SIGNAL-IQ FIELD sync endpoint contract (P3)

**Status:** CONTRACT (P3). The table (`landos.field_pins`, mig 024) and resolver
(`landos.fn_resolve_field_pin`, mig 025) are LIVE. The FastAPI route below is the
agreed interface for the phone client (aegis / ccT6) and is wired in P4 — it is
NOT yet mounted on the deployed engine service (route mount + deploy = operator).

## Doctrine

- A field pin is a **signal**, never a candidate or lead until it mints (P6, gated).
- The phone stores a **ray + intent**; the spine resolves it. The phone may sync a
  pin with **no parcel data** — an unresolved pin is a valid terminal state.
- **Single-writer intact:** this endpoint writes `landos.field_pins` only. It never
  writes `public.acquisition_leads` — `sync_to_crm.py` remains the sole CRM writer.
- **Honest-null:** an unresolved pin returns `resolution_method:"none"`, never a
  fabricated parcel. A `govt`-flagged owner returns the flag, never a seller name.

## Auth

Bearer `LEADS_TOKEN` (single-operator field tool, §8). Per-device revocable tokens
are P7 (🔴 firewall decision). The token is a bearer secret in a URL — acceptable
for one operator, replaced before any VA holds a link.

## `POST /v1/field-pins` — batch sync (idempotent)

Request — an array of captured pins (offline queue flushes here):

```jsonc
{
  "device_id": "iphone-jeramie-01",
  "pins": [{
    "client_pin_id": "<uuid, client-generated — the idempotency key>",
    "tenant_id": "1840ac90-54bd-4b3d-8baf-227c8d84ab5a",
    "recorded_at": "2026-07-14T18:22:10.4Z",   // utterance/tap fired
    "observed_at": "2026-07-14T18:22:07.8Z",   // back-dated sighting (§3.3)
    "origin":   { "lat": 41.6712, "lng": -86.6801 },   // interpolated fix at observed_at
    "origin_accuracy_m": 6.0,
    "heading_deg": 0.0,
    "heading_source": "gps_course",            // gps_course|fix_delta|compass|unknown
    "speed_mps": 20.1,
    "side": "right",                           // left|right|ahead|at|unknown
    "assumed_offset_m": 25,
    "projected": { "lat": 41.6714, "lng": -86.6801 },  // display dot (optional)
    "ray": [[ -86.6801, 41.6712 ], [ -86.6801, 41.6725 ]],  // [origin, origin+150m] lng,lat
    "category": "abandoned",
    "spoken_text": "ping right abandoned house on the corner",
    "operator_label": null
  }]
}
```

Idempotency: `client_pin_id` is UNIQUE per device — a re-flushed pin upserts, never
duplicates (offline-queue acceptance test #7). `pin_not_found`-safe: unknown ids no-op.

Response — each pin echoed with its resolution (the resolver runs synchronously on
insert when the county has geometry; otherwise the pin lands unresolved and is
resolved later):

```jsonc
{
  "synced": 4,
  "results": [{
    "client_pin_id": "…",
    "pin_id": "<server uuid>",
    "resolution": {                            // exactly fn_resolve_field_pin output
      "ok": true,
      "apn": "46-07-07-400-017.000-052",
      "county_fips": "18091",
      "owner": "Lestinsky Steve R Jr & Amy J",
      "owner_flag": "private",                 // private | govt | not_on_file
      "acres": 90.962,
      "geometry": { "type": "MultiPolygon", "coordinates": [ … ] },
      "resolution_method": "ray_intersect",    // ray_intersect | nearest_parcel | none
      "confidence": 0.90,                       // 0.90 ray · 0.50 nearest · null none
      "in_crm": { "present": false }            // {present, lead_id, stage, card_state}
    }
  }]
}
```

Dark-county pin (no geometry): `resolution.resolution_method:"none"`,
`coverage:"dark_county"`, `apn:null`, `geometry:null` — the phone renders
`NO PARCEL DATA`, never a fake plat (acceptance test #4).

## `POST /v1/field-pins/{pin_id}/resolve` — re-resolve (retroactive)

Calls `landos.fn_resolve_field_pin(pin_id)` and re-stamps the pin. The batch
re-resolver for a newly-covered county iterates the partial index
`field_pins_unresolved` (`where resolved_apn is null`). This is how every old
Berrien ray resolves the day Berrien geometry lands — nothing is re-driven.

## Not in P3 (named gates)

- **Live parcel overlay + `in_crm` badge on the phone** — P4 (depends on this).
- **Photo / audio bucket** — P5 (RLS'd Supabase bucket, internal-only).
- **Mint `field_pins → owner_leads → sync_to_crm` + `field_observation` scoring
  weight** — P6, 🔴 **OPERATOR GREEN** (scoring semantics = the moat; a field
  observation gets a weight set once, deliberately, with a number — never smuggled
  in as a UI side effect).
- **Per-device tokens + VA attribution + revocation** — P7, 🔴 firewall decision
  (§11.1: does APG get the app or only the output? position: the output).
