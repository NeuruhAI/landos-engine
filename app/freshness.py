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
