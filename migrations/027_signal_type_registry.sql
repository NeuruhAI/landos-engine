-- 027_signal_type_registry.sql — the LENS DOCTRINE registry (W0, additive).
--
-- Gate-in-data, not application logic: every signal/mine source_type DECLARES
-- its class and lens here, once, so the mint and the recipes route by data.
--   signal_class  ENRICHMENT (match existing lead, modify score)
--                 DISCOVERY  (resolve to APN, MINT a new candidate)
--                 ATTRIBUTE  (a parcel property, not a signal)
--   lens          a portfolio/thesis with its OWN exits and its OWN recipe.
--                 DISTRESS | INCOME_UNDER_ASSET | FLOOD | ENVIRONMENTAL |
--                 OPERATOR_DISTRESS | CONVERSION | ENTITY_DISTRESS
--
-- LENS DOCTRINE 2026-07-14: FLOOD is its OWN lens (HMGP buyout / elevate-resell /
-- land-value / conservation exits), NOT an attribute — that call is reversed here.
-- Lens assignment is routing only; each lens's SCORING recipe is a separate
-- operator-green (with numbers). This table sets no weights.

create table if not exists landos.signal_type_registry (
  source_type   text primary key,
  signal_class  text not null check (signal_class in ('enrichment','discovery','attribute')),
  lens          text check (lens in ('DISTRESS','INCOME_UNDER_ASSET','FLOOD',
                                      'ENVIRONMENTAL','OPERATOR_DISTRESS','CONVERSION',
                                      'ENTITY_DISTRESS')),
  mints         boolean not null default false,   -- discovery types mint; enrichment/attribute do not
  note          text,
  created_at    timestamptz not null default now()
);

insert into landos.signal_type_registry (source_type, signal_class, lens, mints, note) values
  -- DISCOVERY — resolve to APN, mint a candidate
  ('ground_lease',          'discovery', 'INCOME_UNDER_ASSET', true,  'fee under the tower — hold/income; carrier carried from registrant'),
  ('flood_remap',           'discovery', 'FLOOD',              true,  'SFHA parcels — HMGP buyout / elevate-resell / land-value / conservation'),
  ('taxsale',               'discovery', 'DISTRESS',           true,  'in-sale inventory — the original minting source'),
  ('recipe',                'discovery', 'DISTRESS',           true,  'assemblage/curated recipes (e.g. senior_lakefront) — cluster_parcels mint'),
  ('field_pin',             'discovery', 'DISTRESS',           true,  'windshield-sensor observation; scoring weight ungreen (P6)'),
  ('mine_absentee',         'discovery', 'DISTRESS',           true,  'owner-roll: mailing state != parcel state'),
  ('mine_entity',           'discovery', 'ENTITY_DISTRESS',    true,  'owner-roll: entity-owned (owner_kind=entity)'),
  ('mine_exempt',           'discovery', 'CONVERSION',         true,  'owner-roll: church/lodge/VFW/grange/exempt — conversion play'),
  ('mine_same_owner_adjacent','discovery','DISTRESS',          true,  'owner-roll: ST_Touches same-owner (assemblage generalized); lens per operator recipe'),
  -- ENRICHMENT — match an existing lead, modify score
  ('tax_delinquent',        'enrichment','DISTRESS',           false, 'delinquency overlay'),
  ('probate_in',            'enrichment','DISTRESS',           false, 'probate/estate overlay'),
  ('code_violation',        'enrichment','DISTRESS',           false, 'municipal code overlay'),
  ('foreclosure',           'enrichment','DISTRESS',           false, 'sheriff-sale overlay'),
  ('lis_pendens',           'enrichment','DISTRESS',           false, 'pending litigation overlay'),
  ('estate_sale',           'enrichment','DISTRESS',           false, 'estate-sale overlay'),
  ('estate_sale_ext',       'enrichment','DISTRESS',           false, 'estate-sale (extended) overlay'),
  ('heirs_of_tangled_title','enrichment','DISTRESS',           false, 'tangled-title overlay'),
  ('rental_license',        'enrichment','OPERATOR_DISTRESS',  false, 'landlord/operator overlay'),
  ('environmental_part201', 'enrichment','ENVIRONMENTAL',      false, 'MI Part 201 contamination'),
  ('brownfield',            'enrichment','ENVIRONMENTAL',      false, 'EGLE brownfield'),
  ('graph_hub',             'enrichment','ENTITY_DISTRESS',    false, 'portfolio-hub graph overlay'),
  ('graph_shared_mailing',  'enrichment','ENTITY_DISTRESS',    false, 'shared-mailing graph overlay'),
  ('graph_heir_network',    'enrichment','ENTITY_DISTRESS',    false, 'heir-network graph overlay'),
  -- ATTRIBUTE / ungreen — not a minting signal
  ('layoff_notice',         'attribute', 'OPERATOR_DISTRESS',  false, 'WARN — state-grain area-heat; ungreen, no parcel resolver')
on conflict (source_type) do update set
  signal_class = excluded.signal_class, lens = excluded.lens,
  mints = excluded.mints, note = excluded.note;

alter table landos.signal_type_registry enable row level security;
-- RLS deny-all; service_role bypasses (registry read by engine only).
