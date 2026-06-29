# DISARM RECEIPT — tax-sale freshness gate (inverse-of-mint), 2026-06-29

## What happened
A national tax-sale mint lifted **1,290 candidates** (1,203 Delaware/Vanderburgh + 87 St. Joseph)
into the spine. They were **purged** (board returned to 137 = 135 new + 2 qualified;
`landos.discovery_signals` taxsale = 0, `landos.owner_leads` discovered_taxsale = 0). This receipt
records the disarm + the permanent gate so the purge sticks and can never recur.

## Root cause
We ingested **CONSUMED** tax-sale data — lists of sales that **already happened** (Delaware
2025-10-15, Vanderburgh 2025-08-15, St. Joseph 2026-03-13), including parcels already reverted to
the county / already sold. A sale that already occurred is a spent signal everyone already has.
**The edge is UPCOMING sales only** (future auction date = owner under deadline NOW).

## Disarm (immediate)
1. **launchd cron disabled** — `launchctl bootout` + `launchctl disable gui/<uid>/com.neuruh.landos.axon`
   (persists; gone from `launchctl list`). No 6 AM fire can refill the board.
2. **Staged files quarantined** (moved out of the `load_taxsale_county` glob path) →
   `landos-ingest/data/strategy/signals/tax_sale/quarantine/`:
   `delaware_taxsale_signals.jsonl`, `vanderburgh_taxsale_signals.jsonl` (+ README).
3. `stjoseph_taxsale_signals.jsonl` stays in `staged/` (load_signals hardcodes its path) but is now
   **rejected by the gate** (past sale 2026-03-13).

## Permanent gate (so it can NEVER recur, regardless of source)
`app/freshness.py :: upcoming_taxsale(sale_date, detail_json, owner_name, today) -> (ok, reason)` —
single source of truth, imported by the **loaders** and the **minter**. PASS only if the sale_date
is derivable AND in the future (>= today) AND the parcel is not already resolved (reverted/sold/
redeemed / buyer present / amount_paid>0 / surplus>0). No derivable upcoming date → rejected
(gate-in-data). Wired into (loud per-row REJECT logging + a `rejected_stale` counter):
- `scripts/load_signals.py` (taxsale records)
- `scripts/load_taxsale_county.py` (taxsale records)
- `scripts/mint_taxsale_leads.py` (defense-in-depth; `mint_taxsale_inmarket` delegates → inherits it)

Verified by inspection: all three current sources are past-dated → the date rule alone rejects 100%
of them; the reverted/sold guards catch any future-dated-but-already-resolved parcel.

## Open / flagged
- `delaware_commissioner_deed_signals.jsonl` remains in `staged/` — also consumed/past, but NOT
  matched by any loader's glob (`*_taxsale_signals.jsonl`) and the gate would reject it; left in
  place pending your call.
- **DO NOT re-run any cycle** until the new source strategy (upcoming-auction calendars, not
  past-sale lists) is confirmed. The cron stays disabled until then.
