# Switcher Battlecard Quality & Friction Rubric

This document defines the strict engineering rubrics, verification rules, and friction scoring models for constructing an unassailable competitor switcher battlecard.

## 1. Editorial & Technical Tone Standards

- **The Anti-Hyperbole Gate:** Under no circumstances may the battlecard use subjective marketing superlatives ("revolutionary", "game-changing", "world-class", "effortless", "blazing fast"). Every performance or speed claim must be stated in concrete units (e.g. "p99 latency < 15ms under 10k rps", "queries execute over raw ClickHouse columnar storage").
- **The Radical Honesty Mandate:** Technical decision-makers (staff engineers, VP Eng, CTOs) immediately distrust one-sided marketing collateral. The battlecard **MUST** explicitly state at least two areas where the incumbent vendor holds structural superiority (e.g. "Incumbent maintains 600+ turnkey SaaS connectors", "Incumbent holds FedRAMP High certification").
- **Grounded Positioning:** All comparative claims must reflect verified capabilities documented in the repository.

---

## 2. Quantitative Friction Scoring Engine

The following Python model evaluates switching friction and feasibility across lock-in classes. It must be used to calculate friction indexes presented in the analysis.

```python
"""Quantitative friction and feasibility scoring for competitor displacement."""

from typing import Any


def score_migration_feasibility(
    *,
    lock_in_classes: list[str],
    complexity_tier: str,
) -> dict[str, Any]:
    """Score switching feasibility and friction index on a 0-100 scale.

    Raises ValueError on invalid classes or tier.
    """
    valid_classes = {"Class A", "Class B", "Class C", "Class D"}
    valid_tiers = {"low", "medium", "high"}

    if not set(lock_in_classes).issubset(valid_classes):
        raise ValueError("invalid lock-in classes supplied")
    if complexity_tier not in valid_tiers:
        raise ValueError("invalid complexity tier")

    tier_weights = {"low": 10, "medium": 30, "high": 55}
    base_score = 100 - tier_weights[complexity_tier] - (len(lock_in_classes) * 10)
    feasibility_score = max(10, min(100, base_score))

    return {
        "feasibility_score": feasibility_score,
        "risk_level": "low" if feasibility_score >= 70 else ("moderate" if feasibility_score >= 40 else "elevated"),
        "recommended_buffer_days": 3 if complexity_tier == "low" else (7 if complexity_tier == "medium" else 21),
    }
```

---

## 3. Required Report Sections

Every generated `reports/MIGRATION_BATTLECARD.md` must contain these exact Markdown headers:

1. `# [Target Product] vs [Competitor Name]: Switcher Battlecard & Parity Guide`
2. `## Executive Summary & Switching Thesis`
3. `## Incumbent Lock-in & Friction Analysis`
4. `## Honest Architectural Parity Matrix`
5. `## Switcher Positioning & Messaging Guide`
