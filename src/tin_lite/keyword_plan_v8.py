"""Keyword screening with output caps sized as runaway guards, not as expected lengths.

v7 (deployed, so immutable) screens in batches of 50 with an 8,000-token cap and one retry at
16,000. GPT-6 Luna counts reasoning against that cap, and in production the screening calls of
runs a59cded8 and de1a4d3e stopped at exactly 5,000 output tokens after 3.5-4.2k tokens of
reasoning. A cap only stops a run; billing charges the tokens a call actually used, so a tight
cap saves nothing. v8 keeps everything else in v7 (batches, research, seeds, review,
instructions, schemas and the saved-research retry) and changes only the screening caps and
their reservations:

- First attempts get 32,000 output tokens and the one retry 64,000, at least six times the
  largest screening output observed. GPT-6 Luna allows 128,000 output tokens per response
  (https://developers.openai.com/api/docs/models/gpt-6-luna); both caps stay below it.
- Each screening request stays bounded to 80,000 bytes of JSON; Tin's model-usage recorder
  counts those bytes plus 4,096 as input tokens (84,096, standard band). At the pinned rates
  (`service_pricing.CARD`: $0.125 per million for input or cache writes, $0.50 per million
  for output) the largest first attempt costs 84,096 x $0.125/M + 32,000 x $0.50/M = $0.0265
  and the largest retry 84,096 x $0.125/M + 64,000 x $0.50/M = $0.0425. First attempts reserve
  $0.03 and retries $0.045.
- A run reserves at most six batches and six retries ($0.45), so its worst case is seeds
  $0.10, review $0.15, screening $0.45, 22 lookups $1.10 and 40 samples $0.20: $2.00, exactly
  v6's $2 floor, so a ceiling at the floor still never refuses a call.

Seeds (2,000 tokens) and review (24,000 tokens) keep v7's caps: their requests may reach the
long-context band, and raising either to 32,000 would lift the worst case past the $2 floor
that the catalog, the organic system's default keyword limit and the start gate all share.
"""

from __future__ import annotations

from tin_lite import keyword_plan_v7 as v7

# GPT-6 Luna's maximum output tokens per response, from OpenAI's model page.
MODEL_OUTPUT_LIMIT = v7.MODEL_OUTPUT_LIMIT

POLICY = {
    **v7.POLICY,
    "version": "keyword-plan-v8",
    "triage_output_tokens": 32_000,
    "triage_retry_output_tokens": 64_000,
    "triage_reservation_usd": "0.03",
    "triage_retry_reservation_usd": "0.045",
}
INSTRUCTIONS = v7.INSTRUCTIONS
SCHEMAS = v7.SCHEMAS
seed_values = v7.seed_values

assert POLICY["triage_output_tokens"] < POLICY["triage_retry_output_tokens"] <= MODEL_OUTPUT_LIMIT
