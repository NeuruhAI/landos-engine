# HUNT_MINT hang fix — 2026-09-16 (America/Detroit)

## Root cause
After `score_emergence` prints many `lead … emerging_score=` lines, `hunt_mint` called `sync_to_crm.main()` **without** `batch_size`. That path is a single full-table transaction and can sit at ~0% CPU (state S) while `/tmp` mint logs stop growing. `score_emergence` also did N+1 UPDATEs in one unbounded transaction with no lock/statement timeouts.

## Fix (smallest)
1. `scripts/score_emergence.py` — `lock_timeout=15s`, `statement_timeout=180s`; commit every 50 updates; cap summary prints; optional `lead_ids` / `only_fips`; fail-closed on lock/query cancel.
2. `app/hunt_mint.py` — release job DB connection after mint+geocode; scoped `score_emergence`; `sync_to_crm.main(batch_size=100, lead_ids=…)`; fail-closed statuses `mint_failed` / `sync_failed`.

## Prove (Macmini.lan)
Job: `6071b7ad-510b-4d5f-b5d3-918aeb01154c` (`awaiting_operator_mint`, Berrien promoted).
Operator runbook after merge/checkout of this branch:
```bash
cd ~/neuruh-repos/NeuruhAI/landos-engine
git fetch && git checkout fix/hunt-mint-score-emergence-hang
python -m scripts.score_emergence --fips 26021
python -m scripts.sync_to_crm --batch-size 100   # or lead_ids from score
python -m app.hunt_mint 6071b7ad-510b-4d5f-b5d3-918aeb01154c --yes-i-typed-go GO
```
