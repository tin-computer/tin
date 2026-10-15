"""Bounded controller-only Codex token observations; never parse workspace rollouts.

The stop threshold is observed cumulative tokens, not a prepaid dollar ceiling. A
provider request already accepted can finish (and cost) after interruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TOKEN_FIELDS = (
    "totalTokens",
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
)
MAX_OBSERVED_TOKENS = 250_000


@dataclass
class CodexUsage:
    thread_id: str
    turn_id: str
    total: dict[str, int] | None = None
    updates: int = 0
    rejected: bool = False
    limit: int | None = MAX_OBSERVED_TOKENS
    limit_reached: bool = False
    # Subagent threads' latest cumulative tokens: they count toward the same stop.
    subagents: dict[str, int] = field(default_factory=dict)
    _seen: set[tuple[int, ...]] = field(default_factory=set)

    def _combined(self) -> int:
        own = self.total["totalTokens"] if self.total else 0
        return own + sum(self.subagents.values())

    def observe_subagent(self, message: dict[str, Any]) -> bool:
        """Count another thread's tokens (a subagent's) toward the stop; True when they grew."""
        if message.get("method") != "thread/tokenUsage/updated":
            return False
        params = message.get("params")
        if not isinstance(params, dict):
            return False
        thread = params.get("threadId")
        usage = params.get("tokenUsage")
        total = usage.get("total") if isinstance(usage, dict) else None
        value = total.get("totalTokens") if isinstance(total, dict) else None
        if (
            not isinstance(thread, str)
            or not thread
            or thread == self.thread_id
            or type(value) is not int
            or not 0 <= value <= 10**12
        ):
            return False
        if value <= self.subagents.get(thread, -1) or (
            thread not in self.subagents and len(self.subagents) >= 64
        ):
            return False
        self.subagents[thread] = value
        self.limit_reached = self.limit is not None and self._combined() >= self.limit
        return True

    def observe(self, message: dict[str, Any]) -> bool:
        if message.get("method") != "thread/tokenUsage/updated":
            return False
        params = message.get("params")
        if not isinstance(params, dict) or (params.get("threadId"), params.get("turnId")) != (
            self.thread_id,
            self.turn_id,
        ):
            return False
        usage = params.get("tokenUsage")
        total = usage.get("total") if isinstance(usage, dict) else None
        if not isinstance(total, dict):
            self.rejected = True
            return False
        counts = {
            key: total.get(key, 0 if key == "cacheWriteInputTokens" else None)
            for key in TOKEN_FIELDS
        }
        if any(type(value) is not int or not 0 <= value <= 10**12 for value in counts.values()):
            self.rejected = True
            return False
        if (
            counts["totalTokens"] != counts["inputTokens"] + counts["outputTokens"]
            or counts["cachedInputTokens"] > counts["inputTokens"]
            or counts["reasoningOutputTokens"] > counts["outputTokens"]
        ):
            self.rejected = True
            return False
        key = tuple(counts.values())
        if key in self._seen:
            return False
        if self.total and any(counts[key] < value for key, value in self.total.items()):
            self.rejected = True
            return False
        self._seen.add(key)
        self.total = counts
        self.updates += 1
        self.limit_reached = self.limit is not None and self._combined() >= self.limit
        return True

    def record(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "isolated_codex_controller",
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "total": self.total,
            "updates": self.updates,
            "invalid_observation": self.rejected,
            "limit_reached": self.limit_reached,
            "observed_token_limit": self.limit,
            **(
                {
                    "subagent_threads": len(self.subagents),
                    "subagent_tokens": sum(self.subagents.values()),
                }
                if self.subagents
                else {}
            ),
        }
