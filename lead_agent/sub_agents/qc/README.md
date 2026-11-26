# QC agent

**Role.** Scores commenters as leads and flags decision-makers (posts mode).

**Inputs.** One commenter dict (headline, comment text, profile, likes).

**Outputs.** The same dict with `is_decision_maker` and `lead_score` (0-100: decision-maker 40, buying intent in the
comment 30, profile 15, likes up to 15).

**Steps.** `score`.

**Brands.** None. Uses `DECISION_RX` from `core/schema.py`.

**Memory store.** `data/memory/qc.sqlite`.
