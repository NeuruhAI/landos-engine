-- 004_owner_leads_discovery_seed.sql — tag owner-search discovery-seed leads.
--
-- St. Joseph IN (18141) had no signal/lead source when it was stood up, so its 150
-- owner_leads came from ReportAll owner-surname DISCOVERY (a bootstrap seed, surname-
-- biased). Those are NOT live leads — they are kept for the probate match-validator's
-- test set, but must be EXCLUDED from the live ranked queue so real signal-sourced
-- leads (the 12 sheriff-sale foreclosures) are not buried under surname noise.
--
-- discovery_seed=true marks the bootstrap cohort. Scoped to 18141 + provenance
-- 'reportall:%' (the discovery normalizer's provenance). Berrien leads also carry
-- 'reportall:%' provenance but are signal-queue-driven, NOT discovery — so they are
-- explicitly NOT tagged (county scoping protects them).

alter table landos.owner_leads
    add column if not exists discovery_seed boolean not null default false;

update landos.owner_leads
   set discovery_seed = true, updated_at = now()
 where county_fips = '18141'
   and provenance like 'reportall:%'
   and discovery_seed = false;
