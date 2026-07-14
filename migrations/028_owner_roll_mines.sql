-- 028_owner_roll_mines.sql — W0 JOB 2: the owner-roll mines as queryable views.
--
-- Each view is a lens's candidate set drawn from the (widened) landos.parcel_owner
-- roll + landos.parcel_geom. Read-only; minting stays gated (JOB 3, dry-run).
-- Owner matching is EXACT-normalized (upper+btrim), never fuzzy — a fuzzier
-- assemblage match would find more but needs a spot-check gate (RED STOP).
--
-- LIVE (this migration): absentee, entity, exempt/institutional, same-owner-adjacent.
-- BLOCKED (named gaps, no view emitted):
--   * teardown (old + low value + acreage) — year_built AND assessed_value are
--     absent from the sealed source; acreage alone is not teardown.
--   * class-code plays (lodging/self-storage/golf) — property_class here is only
--     529/530/0 (residential/unknown); no commercial subtypes exist to key on.
-- parcel_state is derived from the county FIPS prefix (18->IN, 26->MI); today the
-- roll is LaPorte (18091) only, so every view is LaPorte until more counties load.

-- 1) ABSENTEE — mailing state != parcel state (lens DISTRESS)
create or replace view landos.v_mine_absentee as
select po.county_fips, po.parcel_apn, po.owner_name, po.owner_kind,
       po.owner_mailing_state,
       (case left(po.county_fips,2) when '18' then 'IN' when '26' then 'MI' end) as parcel_state,
       po.acres, po.property_class, po.owner_mailing
from landos.parcel_owner po
where po.owner_mailing_state is not null
  and po.owner_mailing_state <> (case left(po.county_fips,2) when '18' then 'IN' when '26' then 'MI' end);

-- 2) ENTITY-OWNED (lens ENTITY_DISTRESS)
create or replace view landos.v_mine_entity as
select county_fips, parcel_apn, owner_name, owner_kind, owner_mailing_state,
       acres, property_class, vintage_flag
from landos.parcel_owner
where owner_kind = 'entity';

-- 3) EXEMPT / INSTITUTIONAL — church/lodge/VFW/grange + govt (lens CONVERSION)
--    NOTE: an "exempt property class" dimension is BLIND here (class = 529/530/0,
--    no 6xx exempt codes); this mine is name-pattern + govt-flag only.
create or replace view landos.v_mine_exempt_institutional as
select county_fips, parcel_apn, owner_name, property_class, vintage_flag, acres,
       (case
          when owner_name ~* '\y(church|chapel|parish|ministr|congregation|temple|mosque|synagogue|diocese|tabernacle|worship)\y' then 'religious'
          when owner_name ~* '\y(lodge|elks|moose|masonic|odd ?fellows|knights of|shriner|eagles aerie)\y' then 'fraternal_lodge'
          when owner_name ~* '\y(vfw|veterans of foreign|american legion|amvets|legion post)\y' then 'veterans'
          when owner_name ~* '\ygrange\y' then 'grange'
          when vintage_flag = 'govt' then 'govt'
          else 'other'
        end) as institution_kind
from landos.parcel_owner
where vintage_flag = 'govt'
   or owner_name ~* '\y(church|chapel|parish|ministr|congregation|temple|mosque|synagogue|diocese|tabernacle|worship|lodge|elks|moose|masonic|odd ?fellows|knights of|shriner|eagles aerie|vfw|veterans of foreign|american legion|amvets|legion post|grange)\y';

-- 4) SAME-OWNER-ADJACENT — ST_Touches self-join, exact-normalized same owner
--    (assemblage generalized). One row per anchor parcel with a same-owner neighbor.
create or replace view landos.v_mine_same_owner_adjacent as
select a.county_fips, a.parcel_apn, poa.owner_name,
       count(distinct b.parcel_apn)                    as adjacent_same_owner,
       array_agg(distinct b.parcel_apn order by b.parcel_apn) as touching_apns
from landos.parcel_geom a
join landos.parcel_owner poa
  on poa.county_fips = a.county_fips and poa.parcel_apn = a.parcel_apn
join landos.parcel_geom b
  on b.county_fips = a.county_fips and b.parcel_apn <> a.parcel_apn
 and a.geom && b.geom and st_touches(a.geom, b.geom)
join landos.parcel_owner pob
  on pob.county_fips = b.county_fips and pob.parcel_apn = b.parcel_apn
where poa.owner_name is not null
  and upper(btrim(poa.owner_name)) = upper(btrim(pob.owner_name))
  and poa.vintage_flag is distinct from 'govt'          -- govt roads/ROW are not assemblage
group by a.county_fips, a.parcel_apn, poa.owner_name;
