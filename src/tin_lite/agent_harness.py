"""Which agent harness runs a procedure, and the execution contract a run pins for it.

A procedure runs on Codex unless its definition names another harness:

    "procedure": {..., "agent": {"harness": "antigravity", "model": "gemini-4-neon",
                                 "effort": "high"}}

Codex keeps its own contracts, relay and rate cards (codex_api, codex_api_relay,
codex_api_pricing); nothing here changes them. Another harness gets its own pinned contract,
a relay route for its provider's wire format and a rate card, and its runs share the Codex
procedure's admission, receipts, ceilings, sandbox, delivery and recovery.

Antigravity (Google's `agy` CLI) runs Gemini through Tin's Gemini relay: the sandbox holds a
run-bound grant as its API key and the relay holds the real key, admits each request against
the run's budget and prices the usage Google reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CODEX = "codex"
ANTIGRAVITY = "antigravity"
# The pinned Antigravity CLI. A rebuilt image with another version is a new contract.
ANTIGRAVITY_VERSION = "1.5.16"
ANTIGRAVITY_SHA512 = (
    "3dee9f26e298156db673f6ce2a0f5dfed5b30006e04ceb9809c68070e7b717587b33a3da0f8bc2eda45af81a"
    "50bc8b14c82b28ae5832bcfe3e7fd49397117b31"
)
ANTIGRAVITY_URL = (
    "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
    "1.5.16-5250855658717184/linux-x64/cli_linux_x64.tar.gz"
)
ANTIGRAVITY_PROTOCOL = "tin-agy-v1"
# Models a procedure may name for each harness other than Codex, with the efforts it accepts.
MODELS = {ANTIGRAVITY: {"gemini-4-neon": ("low", "medium", "high", "xhigh")}}
DEFAULT_EFFORT = "high"
# What an Antigravity run may do, as runaway guards. Neon's context in Antigravity is 512K
# tokens and one response writes at most 65,536. A request carries the whole conversation
# (text, images Antigravity reads, tool results), so its body is bounded generously.
_ANTIGRAVITY_BOUNDS = {
    "max_request_bytes": 32 * 1024 * 1024,
    "max_output_tokens": 65_536,
    "context_window": 512_000,
    # Subagents and Antigravity's own helper calls (web search, its tool-call rater) run
    # beside the lead; every request still reserves or is admitted against the budget.
    "parallel_requests": 8,
}
# Per-request reservations need lifetime stops; session-funded runs are bounded by their
# ceiling. Both are far above a real run: they stop a loop, not work.
_ANTIGRAVITY_OPERATION_STOPS = {"max_requests": 4000, "max_observed_tokens": 200_000_000}
# Antigravity runs get at least this long when their definition declares less.
MIN_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class AgentSelection:
    harness: str
    model: str
    effort: str = DEFAULT_EFFORT

    def definition(self) -> dict[str, str]:
        return {"harness": self.harness, "model": self.model, "effort": self.effort}


def parse_agent(value: object) -> AgentSelection | None:
    """A procedure's declared harness. None (absent) means Codex, the default."""
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= {"harness", "model", "effort"}:
        raise ValueError("procedure agent must be an object with harness, model and effort")
    harness = value.get("harness")
    if harness not in MODELS:
        raise ValueError("procedure agent harness is unsupported")
    model = value.get("model")
    if model not in MODELS[harness]:
        raise ValueError(f"procedure agent model is not available on {harness}")
    effort = value.get("effort", DEFAULT_EFFORT)
    if effort not in MODELS[harness][model]:
        raise ValueError(f"procedure agent effort is not available for {model}")
    return AgentSelection(harness=harness, model=model, effort=effort)


def selection_of(definition: dict[str, Any]) -> AgentSelection | None:
    procedure = definition.get("procedure")
    return parse_agent(procedure.get("agent")) if isinstance(procedure, dict) else None


def contract(selection: AgentSelection, *, session: bool) -> dict[str, Any]:
    """The execution contract a new admission pins for this harness and model."""
    if selection.harness != ANTIGRAVITY:
        raise ValueError("only non-Codex harnesses have agent contracts")
    value = {
        "mode": "gemini_api_v1",
        "harness": ANTIGRAVITY,
        "harness_version": ANTIGRAVITY_VERSION,
        "provider": "gemini",
        "model": selection.model,
        "effort": selection.effort,
        "protocol": ANTIGRAVITY_PROTOCOL,
        **_ANTIGRAVITY_BOUNDS,
    }
    return value if session else {**value, **_ANTIGRAVITY_OPERATION_STOPS}


CONTRACTS = tuple(
    contract(AgentSelection(ANTIGRAVITY, model, effort), session=session)
    for model, efforts in MODELS[ANTIGRAVITY].items()
    for effort in efforts
    for session in (True, False)
)


def is_agent_contract(value: object) -> bool:
    return isinstance(value, dict) and value in CONTRACTS


def is_agent_session_contract(value: object) -> bool:
    return is_agent_contract(value) and "max_requests" not in value


def relay_url(public_url: str, run_id: object, value: dict[str, Any]) -> str:
    """Where the sandbox sends this run's model requests (its provider's base URL)."""
    if value.get("harness") == ANTIGRAVITY:
        return f"{public_url.rstrip('/')}/internal/agent-api/{run_id}/gemini"
    raise ValueError("unknown agent harness")


def agent_ready_line(value: dict[str, Any]) -> str:
    """What the image's isolation helper prints when it carries this contract's harness."""
    return f"TIN_AGENT_READY_V1 {value['harness']} {value['harness_version']}"


def model_slug(value: dict[str, Any]) -> str:
    """Antigravity names a model and its thinking level in one slug."""
    return f"{value['model']}-{value['effort']}"


# Gemini's Standard-tier list prices, checked against https://aistudio.google.com/docs/pricing
# on 2026-11-24. Antigravity calls Neon for the agent and two helper models of its own: a
# Flash-Lite call that runs Google Search for its web-search tool and rates tool calls, and a
# Gemini 3.8 Flash call (titles). They are priced like any other request; a model missing here
# is refused, never priced as another. Google bills Search grounding per query the model runs
# ($14 per 1,000 after a shared monthly allowance Tin does not pass through). Neon and the
# helpers have no long-context band and implicit caching only, so no cache writes. Output
# includes thinking tokens. Gemini 3.8 Flash's price doubles on 2027-01-01: a new card then.
GEMINI_AGENT_CARD = {
    "id": "gemini-agent-standard-2026-11-24-v1",
    "provider": "gemini",
    "model": "gemini-4-neon",
    "service_tier": "standard",
    "source": "https://aistudio.google.com/docs/pricing",
    "unit": "USD_nanodollars_per_token",
    "models": {
        "gemini-4-neon": {"input": 3_000, "cached_input": 150, "output": 15_000},
        "gemini-3.1-flash-lite-preview": {"input": 250, "cached_input": 25, "output": 1_500},
        "gemini-3.1-flash-lite": {"input": 250, "cached_input": 25, "output": 1_500},
        "gemini-3.8-flash": {"input": 750, "cached_input": 75, "output": 3_750},
    },
    "search_query_nanos": 14_000_000,
}
AGENT_CARDS = (GEMINI_AGENT_CARD,)
# One request's reservation counts this many Search queries; Antigravity's search helper runs
# one or two. A request that runs more is still priced in full; Tin absorbs the excess.
RESERVED_SEARCH_QUERIES = 8


def card_for(selection: AgentSelection) -> dict[str, Any]:
    if selection.harness == ANTIGRAVITY:
        return GEMINI_AGENT_CARD
    raise ValueError("unknown agent harness")


def request_maximum(value: dict[str, Any], card: dict[str, Any] = GEMINI_AGENT_CARD) -> int:
    """One request's customer liability: a full context uncached, a maximum response, and
    the reserved Search queries, at the pinned model's price (the dearest on the card)."""
    rates = card["models"][value["model"]]
    return (
        value["context_window"] * rates["input"]
        + value["max_output_tokens"] * rates["output"]
        + RESERVED_SEARCH_QUERIES * card["search_query_nanos"]
    )


def price_response(card: object, record: dict[str, Any]):
    """Only complete, internally consistent Gemini facts admit a price."""
    if card not in AGENT_CARDS or not isinstance(card, dict):
        return None
    if (
        record.get("outcome") == "upstream_rejected"
        and record.get("pricing") == card
        and record.get("usage") is None
        and type(record.get("upstream_status")) is int
        and record["upstream_status"] != 200
    ):
        # Google refused the request outright (an HTTP error, no response): nothing was
        # generated or billed. A broken or unconfirmed stream never reaches this branch.
        return 0, {
            "rate_card": card["id"],
            "basis": "published_api_list_price",
            "provider": card["provider"],
            "outcome": "upstream_rejected",
            "upstream_status": record["upstream_status"],
            "model_nanos": 0,
            "tool_nanos": 0,
        }
    usage = record.get("usage")
    model = record.get("model")
    if (
        record.get("pricing") != card
        or record.get("provider") != card["provider"]
        or model not in card["models"]
        or record.get("service_tier") not in (None, card["service_tier"])
        or record.get("outcome") != "response_received"
        or record.get("endpoint") not in {"streamGenerateContent", "generateContent"}
        or not isinstance(usage, dict)
    ):
        return None
    names = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    inputs, cached, outputs, total = (usage.get(name) for name in names)
    queries = usage.get("search_queries")
    if any(type(n) is not int or n < 0 for n in (inputs, cached, outputs, total, queries)):
        return None
    if cached > inputs or total != inputs + outputs:
        return None
    rates = card["models"][model]
    model_nanos = (
        (inputs - cached) * rates["input"]
        + cached * rates["cached_input"]
        + outputs * rates["output"]
    )
    tool_nanos = queries * card["search_query_nanos"]
    return model_nanos + tool_nanos, {
        "rate_card": card["id"],
        "basis": "published_api_list_price",
        "provider": card["provider"],
        "model": model,
        "service_tier": card["service_tier"],
        "context_band": "standard",
        "usage": usage,
        "model_nanos": model_nanos,
        "tool_nanos": tool_nanos,
        "outcome": record["outcome"],
    }
