"""
freshness.py — the TAX-SALE FRESHNESS GATE (gate-in-data, permanent).

Root problem this exists to prevent: we ingested CONSUMED tax-sale data — lists of sales that
ALREADY HAPPENED (2025 / early-2026), including parcels that already reverted to the county or
already sold. A sale that already occurred is a spent signal everyone already has. The only edge
is an UPCOMING sale: a future auction date means the owner is under a deadline RIGHT NOW.

`upcoming_taxsale()` is the single source of truth, imported by every tax-sale loader AND the
minter, so a past/resolved parcel can NEVER become a candidate regardless of source. It returns
(ok, reason) so callers log loudly WHAT they rejected and WHY (gate-in-data: a signal with no
derivable upcoming date is rejected, not minted — never fabricated into the pipeline).

PASS only if BOTH:
  * sale_date is derivable AND in the future (>= today — an auction today is still imminent), and
  * the parcel is not already resolved (reverted to govt / sold / redeemed / has a buyer / paid).
"""
from datetime import date, datetime

# Status/result tokens that mean the parcel is already GONE (resolved) — reject even if a future
# date is somehow attached. Conservative on purpose (the date rule does the heavy lifting); these
# only catch future-dated-but-already-resolved anomalies.
RESOLVED_TOKENS = ("COUNTY", "REDEEM", "STRUCK", "DEEDED", "SURPLUS", "SOLD", "CANCEL", "REMOVED")


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def upcoming_taxsale(sale_date, detail_json, owner_name, today):
    """(ok: bool, reason: str). Pass ONLY upcoming, unresolved tax-sale parcels.

    sale_date  — top-level event_date (loader) or discovery_signals.signal_date (minter); str/date.
    detail_json— the staged detail_json / discovery_signals.raw_payload dict (carries .raw fields).
    owner_name — used as a last-resort reverted-to-government check.
    today      — date.today() passed in by the caller (single clock per run).
    """
    dj = detail_json or {}
    raw = dj.get("raw") or {}

    d = _to_date(sale_date)
    if d is None:
        return (False, "no derivable sale date")
    if d < today:
        return (False, f"past sale {d.isoformat()} < today {today.isoformat()} (consumed)")

    # already-resolved guards (future-dated but already gone)
    status_blob = " ".join(str(x) for x in (
        dj.get("status"), dj.get("sale_status"), dj.get("sale_result"),
        raw.get("Status"), raw.get("STATUS"), raw.get("Sale_Result"),
        raw.get("USER_Sale_Result"), raw.get("PROP_STATE"),
    ) if x).upper()
    for tok in RESOLVED_TOKENS:
        if tok in status_blob:
            return (False, f"already-resolved (status~='{tok}': {status_blob[:60]!r})")

    buyer = (dj.get("buyer_name") or raw.get("Buyer_Name") or raw.get("Buyer")
             or raw.get("USER_Buyer_Name") or raw.get("USER_Buyer") or "")
    if str(buyer).strip():
        return (False, f"buyer present (already sold): {str(buyer).strip()!r}")

    paid = _num(dj.get("amount_paid") if dj.get("amount_paid") is not None
                else raw.get("Amount_Pai") if raw.get("Amount_Pai") is not None
                else raw.get("Amount_Paid"))
    if paid is not None and paid > 0:
        return (False, f"amount_paid>0 (already sold): {paid}")

    surplus = _num(dj.get("surplus") if dj.get("surplus") is not None else raw.get("Surplus"))
    if surplus is not None and surplus > 0:
        return (False, f"surplus>0 (sale already settled): {surplus}")

    return (True, f"upcoming {d.isoformat()}")


# ---------------------------------------------------------------------------
# CONTACT-FIELD FRESHNESS (B1, 2026-09-02). Rules, not ML. Published as
# docs/FRESHNESS_RULES.md; the DeedSonar RPC and export mirror these constants
# so the screen, the SQL and the CSV agree to the day.
#
# Every contact-bearing field carries: source, source_as_of (the date the source
# says the value is true), observed_at (when we read it), confidence,
# provenance_url. staleness_days = today - source_as_of. Unknown stays unknown:
# a field without source_as_of is STALE by rule, never given a default date.
# ---------------------------------------------------------------------------
FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"
UNKNOWN_AS_OF = "UNKNOWN"   # reported alongside STALE so "unknown" is never hidden

#: field -> (fresh_max_days, aging_max_days). Beyond aging_max_days = STALE.
FRESHNESS_RULES = {
    "phone":          (90, 365),
    "mailing_address": (180, 365),
    "email":          (180, 365),
    "heir_link":      (365, 730),
    "owner_name":     (365, 730),
    "assessed_value": (365, 730),
    "arrears":        (90, 365),
    "deed":           (365, 730),
}
DEFAULT_RULE = (90, 365)


def staleness_days(source_as_of, today):
    """Days between the source's as-of date and today; None when as-of is unknown."""
    d = _to_date(source_as_of)
    t = _to_date(today)
    if d is None or t is None:
        return None
    return (t - d).days


def field_freshness(field, source_as_of, today):
    """(state, staleness_days, as_of_known) for one contact-bearing field.

    FRESH  : staleness <= fresh_max_days
    AGING  : fresh_max_days < staleness <= aging_max_days
    STALE  : staleness > aging_max_days, a negative (future) as-of, or unknown as-of
    """
    fresh_max, aging_max = FRESHNESS_RULES.get(field, DEFAULT_RULE)
    days = staleness_days(source_as_of, today)
    if days is None:
        return STALE, None, False
    if days < 0:
        return STALE, days, True
    if days <= fresh_max:
        return FRESH, days, True
    if days <= aging_max:
        return AGING, days, True
    return STALE, days, True


def row_freshness_score(fields, today):
    """Row score from its fields: {field: source_as_of}. The row is FRESH only when
    every present contact field is FRESH; AGING when none is STALE; else STALE.
    A row with no contact field at all is STALE (nothing is known to be current)."""
    states = [field_freshness(f, as_of, today)[0] for f, as_of in fields.items()]
    if not states:
        return STALE
    if all(s == FRESH for s in states):
        return FRESH
    if any(s == STALE for s in states):
        return STALE
    return AGING
