"""Keyword screening in batches, with one larger retry when a batch runs out of output.

Production run a59cded8 (keyword-plan-v6) screened its candidates in one call capped at 5,000
output tokens. GPT-6 Luna counts reasoning against that cap: the call reasoned for 4,221
tokens, stopped mid-JSON at 5,000, and the run failed after buying $0.42 of research. v7 keeps
v6's research, seeds, review, instructions and schemas and changes only screening:

- At most 50 candidates per call. One label is about 8 tokens of JSON (`"k50":"wrong_buyer",`)
  and the failed call reasoned about 14 tokens per candidate, so a batch needs about 1,100
  tokens. Its 8,000-token cap leaves room for seven times that.
- A batch that stops at its cap (the provider reports `max_output_tokens`, or its JSON does
  not parse) is asked once more with 16,000 tokens, under its own stage and reservation.
  GPT-6 Luna allows 128,000 output tokens per response
  (https://developers.openai.com/api/docs/models/gpt-6-luna); both caps stay far below it.
- Each screening request is bounded to 80,000 bytes of JSON; Tin's model-usage recorder
  counts those bytes plus 4,096 as input tokens. At the pinned standard-band rates
  (`service_pricing.CARD`: $0.125 per million for input or cache writes, $0.50 per million for
  output) the largest call costs (80,000 + 4,096) x $0.125/M + 16,000 x $0.50/M = $0.0185.
  First attempts and retries each reserve $0.02.
- A run reserves at most six batches and six retries ($0.24), so its worst case is seeds
  $0.10, review $0.15, screening $0.24, 22 lookups $1.10 and 40 samples $0.20: $1.79, still
  inside v6's $2 floor.

Reasoning shares the output cap. OpenAI's `max_output_tokens` bounds reasoning and visible
output together, and the Responses API has no separate reasoning-token limit; Tin's router
sends only `reasoning.effort`. Without one, the model picks its own effort, and in a59cded8
reasoning took 4,221 of the 5,000 tokens. So v7 screens at low effort, since screening only
labels keywords, and sizes each cap as a reasoning budget plus an answer budget:

- The answer is at most 50 labels, about 450 tokens; it gets 1,000.
- Reasoning gets the rest: 7,000 on the first attempt, 15,000 on the retry. The caps, and so
  the reservations, are the same 8,000 and 16,000 as before.
- A cut-off batch whose reasoning ran past its budget, leaving the answer less than its 1,000,
  is recorded as cut off by reasoning. The retry more than doubles the reasoning budget and
  keeps the answer's.
- Every screening receipt keeps the call's output and reasoning tokens, a cut-off one too.
"""

from __future__ import annotations

import math

from tin_lite import keyword_plan_v6 as v6
from tin_lite.keyword_plan import ROUTE_KEY
from tin_lite.model_providers import ModelCapability, ModelRoute, ModelUsage, ProviderName

# GPT-6 Luna's maximum output tokens per response, from OpenAI's model page.
MODEL_OUTPUT_LIMIT = 128_000

POLICY = {
    **v6.POLICY,
    "version": "keyword-plan-v7",
    "triage_batch_size": 50,
    "triage_reasoning_effort": "low",
    "triage_answer_tokens": 1000,
    "triage_output_tokens": 8000,
    "triage_retry_output_tokens": 16000,
    "triage_max_request_bytes": 80_000,
    "triage_reservation_usd": "0.02",
    "triage_retry_reservation_usd": "0.02",
}
INSTRUCTIONS = v6.INSTRUCTIONS
SCHEMAS = v6.SCHEMAS
seed_values = v6.seed_values

assert POLICY["triage_output_tokens"] < POLICY["triage_retry_output_tokens"] <= MODEL_OUTPUT_LIMIT
# Each cap is a reasoning budget plus the same answer budget; the retry more than doubles the
# reasoning budget.
REASONING_TOKENS = POLICY["triage_output_tokens"] - POLICY["triage_answer_tokens"]
RETRY_REASONING_TOKENS = POLICY["triage_retry_output_tokens"] - POLICY["triage_answer_tokens"]
assert RETRY_REASONING_TOKENS > 2 * REASONING_TOKENS > 0

# The keyword route, as v7 pins it: screening sends a reasoning effort, so the route says it
# can take one. Earlier policies pinned the same key without that capability and send none.
ROUTE = ModelRoute(
    key=ROUTE_KEY,
    provider=ProviderName.OPENAI,
    model=POLICY["model"],
    capabilities=frozenset(
        {ModelCapability.TEXT, ModelCapability.JSON_SCHEMA, ModelCapability.REASONING_EFFORT}
    ),
)


def batch_count(policy: dict) -> int:
    """The most screening calls a run can need: every candidate slot, a batch at a time."""
    return math.ceil(policy["max_candidates"] / policy["triage_batch_size"])


def batch_stage(index: int, *, retry: bool = False) -> str:
    return f"triage:{index}:retry" if retry else f"triage:{index}"


def batches(candidates: list, size: int) -> list[list]:
    """Consecutive slices in candidate order; their labels concatenate back in that order."""
    return [candidates[start : start + size] for start in range(0, len(candidates), size)]


def truncation_cause(usage: ModelUsage, *, cap: int, answer_tokens: int) -> str:
    """Why a call stopped at its cap: `reasoning` when reasoning ran past its budget and left
    the answer less than `answer_tokens`, `answer` when the answer outgrew its own room, and
    `unknown` when the provider did not report reasoning tokens."""
    if usage.reasoning_tokens is None:
        return "unknown"
    return "reasoning" if cap - usage.reasoning_tokens < answer_tokens else "answer"
