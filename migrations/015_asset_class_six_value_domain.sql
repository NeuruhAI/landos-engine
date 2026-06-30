-- 015_asset_class_six_value_domain.sql
-- Restore the TRUE six-value asset_class domain on public.acquisition_leads and recover the
-- 212 discovered tax-sale rows that CC3 collapsed into the legacy 4-value domain.
--
-- WHY: the live constraint was acquisition_leads_asset_class_check = {land,residential,
-- commercial,multifamily}. CC3 force-collapsed 212 discovered_taxsale rows into it
-- (acreage+waterfront+vacant_land -> 'land' (152), residential (57), commercial (2),
-- unknown -> NULL (1)). The TRUE classification was never lost — it sits in
-- landos.owner_leads.raw_meta->>'asset_class' (the mint stowed it; CC3 did not touch raw_meta).
-- This migration drops the legacy constraint, reclaims the true six from raw_meta, then locks
-- the canonical six-value domain. Recovery target distribution (verified 2026-06-30):
--   acreage 147, residential 57, waterfront 3, vacant_land 2, commercial 2, unknown 1  (= 212)
--
-- NOTE (deviation from directive template): public.acquisition_leads has NO `provenance` column
-- (verified) — provenance lives on landos.owner_leads. The recovery scopes the discovered tier
-- via ol.provenance (the joined owner_leads row), which is the precise equivalent of the
-- directive's al.provenance and matches the 212 discovered rows exactly.
--
-- Reversible: DROP CONSTRAINT acquisition_leads_asset_class_chk; (the legacy values are not
-- restored, by design — the collapse is what we are undoing).

BEGIN;

-- 1. drop the legacy 4-value constraint (exact live name confirmed via pg_constraint)
ALTER TABLE public.acquisition_leads
  DROP CONSTRAINT IF EXISTS acquisition_leads_asset_class_check;

-- 2. recover the 212 discovered_taxsale rows from raw_meta to the TRUE six (no collapse;
--    scraper 'unknown' is reclaimed as 'unknown', NOT NULL; any unexpected variant -> 'unknown')
UPDATE public.acquisition_leads al
SET asset_class = CASE lower(trim(ol.raw_meta->>'asset_class'))
      WHEN 'waterfront'  THEN 'waterfront'
      WHEN 'acreage'     THEN 'acreage'
      WHEN 'vacant_land' THEN 'vacant_land'
      WHEN 'residential' THEN 'residential'
      WHEN 'commercial'  THEN 'commercial'
      WHEN 'unknown'     THEN 'unknown'
      ELSE 'unknown'
    END
FROM landos.owner_leads ol
WHERE al.source_lead_id = ol.lead_id
  AND ol.provenance = 'discovered_taxsale'
  AND ol.raw_meta->>'asset_class' IS NOT NULL;

-- 3. add the canonical 6-value constraint (NULL = legacy/unclassified, allowed)
ALTER TABLE public.acquisition_leads
  ADD CONSTRAINT acquisition_leads_asset_class_chk
  CHECK (asset_class IS NULL OR asset_class IN
    ('waterfront','acreage','vacant_land','residential','commercial','unknown'));

COMMIT;
