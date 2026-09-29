"""The v5 research, sized to what its calls can cost. Instructions, schemas, seeds, sample caps
and model inputs are v5's; only the per-call reservations and the ceiling floor change.

Earlier policies reserved deliberately large fixed amounts ($4 for the review alone), so a run
could not be given a ceiling near its real cost. Each v6 reservation is an upper bound for one
call from published list prices, with headroom, not an invoice:

- DataForSEO Labs (ranked keywords, competitors, suggestions, ideas, related, overview) lists
  $0.012 per task plus $0.00012 per returned item. The largest request returns 200 rows:
  $0.012 + 200 x $0.00012 = $0.036. Reserve $0.05.
- A live organic Google SERP lists $0.002 per page of ten results; each sample reads one
  page. Reserve $0.005.
- GPT-6 Luna, at the rates pinned in `service_pricing.CARD`: model input is capped at
  300,000 bytes, plus at most 5 KB of instructions and 19 KB of output schema (the review
  schema with 300 candidate slots). Counting one token per byte puts a full request in the
  long-context band, so 325,000 tokens x $0.25 per million (the cache-write rate, the highest
  input rate) = $0.081, plus output at $0.75 per million: seeds 2,000 tokens ($0.0015),
  screening 5,000 ($0.0038), review 24,000 ($0.018). Worst cases $0.083, $0.085 and $0.099.
  Reserve $0.10, $0.10 and $0.15.

A full run reserves 3 model calls ($0.35), 22 lookups (target, competitor discovery, three
competitor footprints, seed metrics, and suggestions plus related keywords for eight seeds:
22 x $0.05 = $1.10) and 40 samples (40 x $0.005 = $0.20): $1.65. The $2 floor covers that,
so a ceiling at the floor never refuses a call.
"""

from tin_lite import keyword_plan_v5 as v5

POLICY = {
    **v5.POLICY,
    "version": "keyword-plan-v6",
    "labs_reservation_usd": "0.05",
    "serp_reservation_usd": "0.005",
    "seed_reservation_usd": "0.10",
    "triage_reservation_usd": "0.10",
    "review_reservation_usd": "0.15",
    "minimum_ceiling_usd": "2",
}
INSTRUCTIONS = v5.INSTRUCTIONS
SCHEMAS = v5.SCHEMAS
seed_values = v5.seed_values
