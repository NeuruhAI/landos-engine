
# NC DPS P0 — LandOS Engine Boundary

## Canonical ownership

`landos-engine` is the sole writer for:

- Property-level evidence assembly.
- Preliminary ownership-claim graphs.
- Conservative equity calculations.
- Tax, court, title, probate and physical-distress features.
- Raw DPS scoring.
- Opportunity scoring.
- Urgency scoring.
- Evidence-confidence scoring.
- Composite priority.
- Property and score receipts.
- Approved CRM projection payloads.

## Score separation

The system must retain separate values for:

1. `raw_dps_score`
2. `normalized_dps_score`
3. `opportunity_score`
4. `urgency_score`
5. `evidence_confidence`
6. `composite_priority`

Urgency cannot silently replace opportunity quality. Evidence confidence cannot
be treated as economic value.

## Preliminary ownership graphs

LandOS may organize people, entities, instruments and claimed interests. It
cannot declare someone a legal heir, exclude a possible claimant, or determine
final ownership.

## Hard stops

P0 property packets must explicitly carry false values for contact, skip trace,
offer, payment, filing, recording, clear-title and legal-heir authority.
