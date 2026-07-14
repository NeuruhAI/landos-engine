"""
seller_contact.py — the spine's mirror of the CRM A8.2 write-side invariant
(AnchorPG/apg-land-crm src/lib/leads/seller-contact.ts, read 2026-07-14).

RULING 2026-07-14: sync_to_crm is already the sole writer of public.acquisition_leads;
it now also becomes the SOLE writer of SELLER contact rows DERIVED FROM THE SPINE.
The CRM composer/drips/contracts resolve send targets from public.contacts
(related_lead_id + phone), so a lead phone without a seller contact row is
untextable/unemailable/undrippable. This closes that bridge.

Invariant (mirrors the .ts exactly, plus a consent retract):
  * ONE seller row per lead (related_lead_id + contact_type='seller').
  * first-write-wins on phone — an existing phone (human OR spine) is NEVER
    clobbered.
  * name parsed from owner_name: >1 token -> first=all-but-last, last=last;
    1 token -> first=token, last=NULL.
  * a SUPPRESSED / DNC lead never yields a live contact phone — the consent rail
    wins. The retract touches ONLY spine-owned rows (source='spine_sync'); a
    human-created or human-edited contact is NEVER touched (RED STOP).
  * source='spine_sync' (engine origin), distinct from the CRM's own
    'lead_phone_mirror' and the one-time 'phone_1_backfill'.

Caller (sync_to_crm) passes a psycopg dict_row cursor inside its transaction, so
dry-run rolls back with everything else.
"""
from typing import Optional

SOURCE = "spine_sync"


def parse_name(owner_name: Optional[str]):
    """owner_name -> (first_name, last_name), mirroring the .ts split."""
    parts = (owner_name or "").split()
    if len(parts) > 1:
        return " ".join(parts[:-1]), parts[-1]
    if parts:
        return parts[0], None
    return None, None


def upsert_seller_contact_phone(cur, *, lead_id, tenant_id, owner_name, phone, callable):
    """Upsert (or consent-retract) the lead's seller contact. Returns one of
    'created' | 'filled' | 'left' | 'retracted' | 'noop'. cur is a dict_row cursor."""
    phone = (phone or "").strip() or None

    cur.execute(
        """select contact_id, phone, source from public.contacts
           where related_lead_id = %s and contact_type = 'seller'
           order by created_at asc limit 1""",
        (lead_id,))
    row = cur.fetchone()

    if callable and phone:
        if row:
            if row["phone"]:
                return "left"                       # no clobber (human OR spine)
            cur.execute(
                "update public.contacts set phone=%s, updated_at=now() where contact_id=%s",
                (phone, row["contact_id"]))
            return "filled"
        first, last = parse_name(owner_name)
        cur.execute(
            """insert into public.contacts
                 (tenant_id, contact_type, first_name, last_name, phone,
                  related_lead_id, source)
               values (%s, 'seller', %s, %s, %s, %s, %s)""",
            (tenant_id, first, last, phone, lead_id, SOURCE))
        return "created"

    # suppressed / not-callable: retract ONLY a spine-owned phone (consent rail).
    if row and row["source"] == SOURCE and row["phone"]:
        cur.execute(
            "update public.contacts set phone=null, updated_at=now() "
            "where contact_id=%s and source=%s",
            (row["contact_id"], SOURCE))
        return "retracted"
    return "noop"
