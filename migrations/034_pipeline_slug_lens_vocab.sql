-- 034_pipeline_slug_lens_vocab.sql — HUNT/SCORING FILTER: document the cold-call LENS
-- vocabulary on acquisition_leads.pipeline_slug. Additive + idempotent (a COMMENT only).
--
-- Doctrine: "absentee is a dimension, not a thesis." A bank/LLC/corp/trust that mails
-- off-parcel is a commercial holder, NOT a motivated cold-call seller — so it must not sit
-- in a human cold-call queue. app/lead_visibility.backfill_pipeline_and_coords assigns
-- pipeline_slug from app/score_absentee.cold_call_lens (the single source of truth):
--
--   'land'       -> human SELLER, land play            (cold-call)
--   'distressed' -> human SELLER, distress-led         (cold-call)
--   'entity'     -> institution (bank/LLC/inc/corp/    (NEVER cold-dialed; entity/commercial lens)
--                    trust/authority/facilities/…)
--   'govt'       -> government / land-bank / surplus   (NEVER cold-dialed; disposition channel)
--
-- No hard CHECK is imposed here on purpose: pipeline_slug is written by more than one lane
-- and a strict enum could reject a concurrent writer mid-flight. The vocabulary is enforced
-- in code (lead_visibility) and documented here; harden to a CHECK once the vocab is settled
-- across every writer. card_state is orthogonal and never touched by the relens (reversible).

comment on column public.acquisition_leads.pipeline_slug is
  'Cold-call lens queue: land|distressed = human sellers (cold-call); '
  'entity = institutions (no dial); govt = government/land-bank (disposition). '
  'Assigned by app.score_absentee.cold_call_lens via app.lead_visibility. See migration 034.';
