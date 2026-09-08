"""VALUATION unlock: derive equity.estimated_pct from assessed_value.

Gap class: NORMALIZED_FACT_MISSING (assessed_value is already on the estate).
Unlock mode: DERIVED_FROM_EXISTING.
Never RAW_SOURCE_MISSING. Never a scraper.

Honesty: unknown debt is PENDING, not zero. A missing tax-balance / amount_owed
must not read as 100% equity. The debt side is the tax-balance proxy already
marked proxy in the recipe-engine score vector — not a mortgage search.
"""
from __future__ import annotations

from typing import Any, Optional

from .classes import (
    FAMILY_VALUATION,
    INGREDIENT_EQUITY_PCT,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)
from .numbers import as_number, first_number

ASSESSED_KEYS = (
    "assessed_value",
    "assessed_total",
    "parval",
    "total_assessed",
)
DEBT_PROXY_KEYS = (
    "tax_balance",
    "tax_delinquent_amount",
    "amount_owed",
    "min_bid",
    "minimum_bid",
)

# DEED_TENURE / mortgage search stay out of this derivation.
FORBIDDEN_BASIS_KEYS = (
    "years_owned",
    "last_sale_date",
    "deed_date",
    "mortgage_balance",
    "estimated_equity",
)


def derive_equity_estimated_pct(
    row: Optional[dict] = None,
    *,
    assessed_value: Any = None,
    tax_balance: Any = None,
    amount_owed: Any = None,
) -> dict[str, Any]:
    """Return a labeled equity.estimated_pct fact.

    Formula (proxy): (assessed_value − debt_proxy) / assessed_value × 100
    when both sides are known and assessed_value > 0.
    """
    gap, mode = unlock_classes(FAMILY_VALUATION)
    src = dict(row or {})
    assessed = (
        first_number({"assessed_value": assessed_value}, ("assessed_value",))
        if assessed_value is not None
        else first_number(src, ASSESSED_KEYS)
    )
    debt = None
    debt_kind = None
    explicit_debt = amount_owed if amount_owed is not None else tax_balance
    if explicit_debt is not None:
        debt = as_number(explicit_debt)
        debt_kind = "amount_owed" if amount_owed is not None else "tax_balance"
    else:
        for key in DEBT_PROXY_KEYS:
            if key not in src:
                continue
            n = as_number(src.get(key))
            if n is not None:
                debt, debt_kind = n, key
                break

    base = {
        "ingredient_id": INGREDIENT_EQUITY_PCT,
        "family": FAMILY_VALUATION,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "DERIVED",
        "proxy": True,
        "unit": "percent",
        "formula": "(assessed_value - debt_proxy) / assessed_value * 100",
        "debt_proxy_kind": debt_kind,
        "basis": {
            "assessed_value": assessed,
            "debt_proxy": debt,
            "debt_proxy_kind": debt_kind,
        },
        "source": "assessed_value + tax-balance/amount-owed debt proxy",
    }

    if assessed is None:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "value": None,
            "reason": "ASSESSED_VALUE_ABSENT",
            "why": (
                "equity.estimated_pct is a normalized fact over assessed_value; "
                "this row has no assessed_value. Not a raw-source scrape."
            ),
        }
    if assessed <= 0:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "value": None,
            "reason": "ASSESSED_VALUE_NOT_POSITIVE",
            "why": "assessed_value must be > 0 to derive a percent.",
        }
    if debt is None:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "value": None,
            "reason": "DEBT_PROXY_PENDING",
            "why": (
                "Unknown tax-balance/amount-owed is pending, never treated as $0 "
                "(that would fabricate 100% equity)."
            ),
        }

    pct = round((assessed - debt) / assessed * 100.0, 1)
    return {
        **base,
        "state": STATE_KNOWN,
        "value": pct,
        "reason": "DERIVED_FROM_ASSESSED_AND_DEBT_PROXY",
        "why": (
            f"Derived {pct}% from assessed_value={assessed} and "
            f"{debt_kind}={debt} (tax-balance debt proxy, not a mortgage)."
        ),
    }
