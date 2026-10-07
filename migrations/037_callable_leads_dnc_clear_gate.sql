-- 037_callable_leads_dnc_clear_gate.sql
-- LANDOS-P1.1: structurally gate callable leads on verified DNC clearance.
--
-- WHY (ccT13 finding, 2026-07-15): public.callable_leads (last shaped by CRM
-- migration 080_dnc_tri_state_callable_gate) gates on dnc IS FALSE + opt-outs +
-- consent, but does NOT itself verify the scrub evidence. The invariant
-- "callable <=> provably scrubbed" held only by DATA (43/43 rows happened to
-- join to a dnc_status='clear' owner_lead), not by construction. A future row
-- with dnc=false + a phone could enter the dialer book with no scrub evidence.
--
-- CONTRACT after this migration — a row is callable iff ALL of:
--   1. dnc IS FALSE                       (080 tri-state: explicit clean verdict;
--                                          NULL = never scrubbed = fail closed)
--   2. phone_1 present                    (080)
--   3. no call/all opt-out                (080; an opt-out always wins)
--   4. manual leads carry opted_in call consent   (080; registry != consent)
--   5. NEW — verified DNC-clear evidence: phone_1 (normalized, last 10 digits)
--      appears in the canonical scrub metadata landos.owner_leads.skip_trace_meta
--      ->'phones' with per-phone dnc_status='clear' AND a dnc_scrubbed_at
--      timestamp, on a row whose top-level dnc_status='clear'.
--   6. NEW — not synthetic: phone_1's NANP central-office code (digits 4-6 of
--      the 10-digit number) is not 555. Both live P3 gate fixtures sit in the
--      NANP-reserved fictional block 555-01XX (+1248 555 0103, +1708 555 0101);
--      no real subscriber number is assigned in the 555 exchange. This is a
--      STRUCTURAL rule (approved test-number range), not a name-string match.
--
-- SECURITY: callable_leads is security_invoker=true and authenticated/
-- service_role have NO access to schema landos (engine-side PII custody,
-- migration 019 RLS posture). Evidence is therefore checked through a
-- SECURITY DEFINER boolean function owned by postgres with a locked
-- search_path: invokers learn only pass/fail, never the scrub payload.
--
-- Idempotent: CREATE OR REPLACE only. Additive: no drops, columns unchanged.
-- Rollback: re-apply the prior view definition (captured in
-- CALLABLE_LEADS_HARDENING_RECEIPT.json alongside this change).

-- 1. Evidence oracle: does this (source_lead_id, phone) carry timestamped
--    DNC-clear scrub evidence in the canonical owner-lead metadata?
create or replace function public.fn_dnc_clear_evidence(
  p_source_lead_id uuid,
  p_phone text
) returns boolean
language sql
stable
security definer
set search_path = ''
as $fn$
  select
    -- a usable NANP number normalizes to at least 10 digits
    length(regexp_replace(coalesce(p_phone, ''), '[^0-9]', '', 'g')) >= 10
    and exists (
      select 1
        from landos.owner_leads ol,
             lateral jsonb_array_elements(
               coalesce(ol.skip_trace_meta->'phones', '[]'::jsonb)
             ) as p(entry)
       where ol.lead_id = p_source_lead_id
         and ol.skip_trace_meta->>'dnc_status' = 'clear'
         and p.entry->>'dnc_status' = 'clear'
         and coalesce(p.entry->>'dnc_scrubbed_at', '') <> ''
         and right(regexp_replace(p.entry->>'number', '[^0-9]', '', 'g'), 10)
             = right(regexp_replace(p_phone, '[^0-9]', '', 'g'), 10)
    );
$fn$;

comment on function public.fn_dnc_clear_evidence(uuid, text) is
  'Boolean-only oracle: TRUE iff the phone matches a per-phone dnc_status=clear '
  'entry with a dnc_scrubbed_at timestamp in landos.owner_leads.skip_trace_meta '
  'for that source_lead_id (row-level dnc_status must also be clear). SECURITY '
  'DEFINER so the security_invoker callable_leads view can verify evidence '
  'without granting invokers access to engine PII. 037, LANDOS-P1.1.';

revoke all on function public.fn_dnc_clear_evidence(uuid, text) from public;
revoke all on function public.fn_dnc_clear_evidence(uuid, text) from anon;
grant execute on function public.fn_dnc_clear_evidence(uuid, text)
  to authenticated, service_role;

-- 2. The hardened view. Column list and 080 predicates unchanged; two new
--    conjuncts (evidence + non-synthetic) appended.
create or replace view public.callable_leads
with (security_invoker = true) as
select
  lead_id,
  source_lead_id,
  tenant_id,
  owner_name,
  situs_address,
  county_fips,
  parcel_apn,
  final_lead_score,
  status,
  assigned_to,
  assigned_at,
  assigned_by,
  created_at,
  phone_1,
  phone_1_type,
  phone_2,
  contact_confidence,
  outreach_recipe,
  controlling_human,
  commercial_agent_flag,
  portfolio_hub,
  situs_lat,
  situs_lng,
  dnc,
  call_status,
  last_call_at,
  call_notes,
  asset_class,
  pipeline_slug,
  signal_tags,
  source,
  amount_owed,
  market_value_est,
  equity_pct,
  taxes_annual
from public.acquisition_leads al
where dnc is false
  and nullif(btrim(phone_1), '') is not null
  and not exists (
        select 1
          from comms.opt_outs o
         where o.lead_id = al.lead_id
           and o.channel = any (array['call'::text, 'all'::text])
      )
  and (
        source <> 'manual'::text
        or coalesce((
             select c.consent_status = 'opted_in'::text
               from comms.consent c
              where c.lead_id = al.lead_id
                and c.channel = 'call'::text
              order by coalesce(c.captured_at, c.created_at) desc,
                       c.created_at desc,
                       c.consent_id desc
              limit 1
           ), false)
      )
  -- 037: callable requires verified, timestamped DNC-clear scrub evidence
  and public.fn_dnc_clear_evidence(al.source_lead_id, al.phone_1)
  -- 037: synthetic/test fixtures never dial — NANP 555 exchange is reserved
  -- (fictional block 555-01XX); structurally excluded regardless of metadata
  and substr(right(regexp_replace(phone_1, '[^0-9]', '', 'g'), 10), 4, 3)
      <> '555';

comment on view public.callable_leads is
  'Dialer book. callable <=> dnc=false (explicit verdict; NULL fails closed) '
  'AND phone present AND no call/all opt-out AND (non-manual OR opted_in call '
  'consent) AND verified timestamped DNC-clear evidence in landos.owner_leads '
  'scrub metadata (fn_dnc_clear_evidence) AND phone not in the NANP 555 test/'
  'fictional exchange. 080 established the tri-state dnc contract; 037 made '
  'the scrub-evidence requirement structural. LANDOS-P1.1.';
