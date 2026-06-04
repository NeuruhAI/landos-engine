-- 003_owner_leads_reportall_parcel_id.sql — Indiana ingest-id / match-id split.
--
-- Multi-county reality: the ingest key and the match key are NOT the same field in
-- every county.
--   * Berrien MI: ReportAll parcel_id == the human county APN (minus the 11- prefix);
--     parcel_apn holds it and is also the ingest lookup key.
--   * St. Joseph IN (18141): ReportAll parcel_id is an INTERNAL id (018-form, e.g.
--     018-8034-1460) used only to LOOK UP the record; the human county tax APN is in
--     alt_id_1 (71-...-026 form). We store alt_id_1 in parcel_apn (the canonical match
--     key, consistent across counties) and keep the 018-form ingest id here.
--
-- This column is the ReportAll-native ingest id. Nullable: Berrien rows don't need it
-- (their parcel_apn already is the ReportAll parcel_id). landos schema only; additive.

alter table landos.owner_leads
    add column if not exists reportall_parcel_id text;
