# Geometry Infrastructure Spec — parcel polygons, PIP, area-heat

**Status:** SPEC ONLY (approved 2026-07-09, NOT built). This is the design of record
for when geometry becomes load-bearing. Do not implement until a build trigger fires.

## Build triggers (either one)
1. **First live deal in a flood or tower geography** — the moment a `flood_remap` or
   `ground_lease` signal needs to resolve to a specific host parcel for an actual deal.
2. **Bryan asks for the map layer** — a polygon overlay on `/map` (parcel shapes, flood
   zones, WARN geographies) rather than today's point pins.

Until then, Jobs 7's disarm-with-named-gap stands (ratified): `flood_remap` (45),
`indiana_warn` (1,163), `ground_lease` (224) are held as facts, NOT scored, because the
$0 point-in-polygon resolve they need has no geometry to run against yet.

## Why geometry is the blocker (recap of the Job-7 gap)
- PostGIS is **not installed** on the spine.
- There is **no parcel-polygon layer** — `landos.overlay_polygons` holds only 19
  non-parcel overlays; `owner_leads` / `parcel_meta` carry a point (situs_lat/lng) and an
  APN string, no boundary.
- So a tower point (`ground_lease`) cannot be resolved to the host parcel it sits on, and
  a flood-remap community polygon cannot be intersected with the parcels it newly covers.
  Both are point-in-polygon (PIP) operations; PIP needs polygons.

---

## Part 1 — PostGIS install (one-time, RED: operator-fired)

PostGIS is a Postgres extension; enabling it is a DB-role/DDL action = **RED** under
Autonomy Policy v1. Agent prints, operator fires.

```sql
-- on lvdi (lvdimujkfheeyqchjzra), operator-run:
create extension if not exists postgis;        -- geometry types + GIST + ST_* functions
create extension if not exists postgis_topology; -- only if topology ops are needed (defer)
```

Supabase ships PostGIS in its image; `create extension` is the whole install. Verify:
```sql
select postgis_full_version();
select default_version from pg_available_extensions where name = 'postgis';
```

No app-code dependency is added: the engine talks to geometry through SQL (`ST_Contains`,
`ST_Intersects`), so `psycopg` needs nothing new. Reads of `geometry` come back as WKB/EWKT
text unless we `ST_AsGeoJSON(...)` at the query — prefer GeoJSON at the SQL boundary so the
Python side never parses WKB.

---

## Part 2 — Parcel-polygon sourcing ($0, county ArcGIS REST — t5's ingest lane)

Parcel boundaries are **free** from county/state ArcGIS REST FeatureServers (the same $0
feeds t5 already mines for the assessor detail capture). No paid parcel vendor.

- **St. Joseph County, IN** — the county publishes a parcel FeatureServer on its ArcGIS Hub
  (same host family as the `stjoseph_code_violation` layer already armed). Query
  `.../FeatureServer/<id>/query?where=1=1&outFields=PARCEL_ID&outSR=4326&f=geojson`,
  paginate with `resultOffset`/`resultRecordCount` (default page cap 1000–2000).
- **Indiana statewide** — IGIO / IndianaMap publishes a statewide parcel layer; usable as
  a fallback where a county has no own server.
- **Berrien County, MI** — GAP (BS&A bans automation, per the assessor-capture memo).
  Michigan parcels for our MI counties come from the state GIS Open Data portal where
  published; otherwise this county stays geometry-dark (honest gap, same as its assessor
  data).

**Projection:** request `outSR=4326` (WGS84) so geometry lands in the same CRS as our
situs points and MapLibre. Store as `geometry(MultiPolygon, 4326)` (parcels are sometimes
multi-ring). Reproject to a metre CRS (e.g. `ST_Transform(geom, 5070)` CONUS Albers) only
inside area/containment math, never at rest.

### Handoff contract — the geometry table t5 must deliver
t5 owns ingest; this is the exact table shape the engine needs. **Additive, own-schema,
idempotent** — mirrors the code_violation lane.

```sql
-- migration (t5-authored, landos schema): parcel boundary layer
create table if not exists landos.parcel_geom (
  id          uuid primary key default gen_random_uuid(),
  county_fips text not null,                       -- join key half #1 (matches owner_leads)
  parcel_apn  text not null,                       -- join key half #2 (canonical, 71-form etc.)
  geom        geometry(MultiPolygon, 4326) not null,
  centroid    geometry(Point, 4326),               -- ST_PointOnSurface(geom); cheap PIP prefilter
  source      text not null,                       -- 'arcgis:stjoseph_parcels' etc. (provenance)
  source_ref  text,                                -- county OBJECTID / GlobalID — idempotency
  ingested_at timestamptz not null default now(),
  unique (county_fips, parcel_apn)                 -- one boundary per parcel; re-run upserts
);
create index if not exists parcel_geom_gix        on landos.parcel_geom using gist (geom);
create index if not exists parcel_geom_centroidix on landos.parcel_geom using gist (centroid);
create index if not exists parcel_geom_apn_idx    on landos.parcel_geom (county_fips, parcel_apn);
```

**Contract terms:**
1. **Join key = `(county_fips, parcel_apn)`** — identical to the seam sync + the resolver
   use, so a parcel_geom row lines up with its `owner_leads` row with zero fuzzy matching.
   t5 MUST canonicalize `parcel_apn` to the SAME form `owner_leads` stores (e.g. St. Joseph
   71-form) before insert, or the join silently misses.
2. **CRS = 4326 at rest**, MultiPolygon, non-null `geom`. Centroid precomputed
   (`ST_PointOnSurface`, which is guaranteed inside the polygon, unlike `ST_Centroid`).
3. **Provenance mandatory** (`source`, `source_ref`) and **idempotent** on
   `(county_fips, parcel_apn)` — re-ingest converges, never duplicates.
4. **Honest coverage:** t5 reports per-county load counts (e.g. "St. Joseph 118k/119,823");
   counties with no free server are logged as a named gap, never faked.
5. **No engine writes to this table** — t5 (ingest) is its sole writer; the engine reads
   only. Same firewall posture as `discovery_signals`.

---

## Part 3 — Point-in-polygon resolve for `ground_lease` (tower → host parcel)

A `ground_lease` signal is a cell-tower / billboard point. It monetizes only when tied to
the parcel it sits on (the host owner collects — or could collect — the lease). PIP:

```sql
-- resolve each tower point to the parcel whose polygon contains it
select t.tower_id, g.county_fips, g.parcel_apn
from landos.discovery_signals t
join landos.parcel_geom g
  on ST_Contains(g.geom, ST_SetSRID(ST_MakePoint(t.lng, t.lat), 4326))
where t.source_type = 'ground_lease' and t.matched_lead_id is null;
```

Then set `discovery_signals.matched_lead_id` = the `owner_leads.lead_id` for that
`(county_fips, parcel_apn)` — the tower becomes a distress/opportunity token on the host
lead, scored through the existing composite path (no new scorer axis; it rides `distress`
or a `strategy_fit` bump per the recipe). Ambiguity guard: if a point lands in ≥2 polygons
(overlapping/bad geometry), leave `matched_lead_id` NULL and log — never guess (same
unambiguous-only rule as `match_temporal_signals`).

---

## Part 4 — Community-polygon resolve for `flood_remap` (area-heat, Job 7b)

A `flood_remap` (FEMA map revision) is a **polygon**, not a point: a community/zone whose
flood designation changed. It touches every parcel it now overlaps — that is **area-heat**.

```sql
-- every parcel intersecting a newly-remapped flood polygon
select g.county_fips, g.parcel_apn, f.zone, f.effective_date
from landos.overlay_polygons f
join landos.parcel_geom g
  on ST_Intersects(g.geom, f.geom)
where f.kind = 'flood_remap';
```

Each intersected parcel gets an **area-heat modifier** — NOT a minted parcel, NOT a new
distress axis. It behaves like the Job-8 outcome modifier: a post-floor multiplier (or a
`strategy_fit` flag) on parcels already in the book, provenance-stamped
`inputs_snapshot.area_heat = {source:'flood_remap', zone, polygon_ref}`. `indiana_warn`
(plant-closure WARN geographies) resolves the same way once its geography is a real polygon
rather than the coarse statewide `geography="18"` stub.

**Explicitly rejected (per ruling):** a zip-crosswalk approximation of area-heat. Do NOT
map WARN/flood to parcels by zip centroid or county rollup — that fabricates precision the
data doesn't have. Area-heat waits for real polygons + real PIP. Until then it stays
disarmed-with-named-gap.

---

## Sequence when a trigger fires
1. Operator fires `create extension postgis` (RED).
2. t5 ships `landos.parcel_geom` + the ArcGIS ingest for the deal's county (per contract).
3. Engine adds the PIP resolvers (Parts 3–4) — additive scripts, dry-run first, same
   care as `match_temporal_signals`.
4. `ground_lease` / `flood_remap` / `indiana_warn` move from held-facts to scored, each as
   a provenance-stamped post-floor modifier. Push per landed step.
