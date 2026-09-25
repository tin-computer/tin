# Switching Friction & Lock-in Taxonomy

This document formalizes the mechanical vectors through which incumbents create artificial switching friction, and the structural levers required to neutralize them in a competitive battlecard.

## 1. Incumbent Lock-in Classification

Incumbent vendors sustain retention through four structural categories of friction. Every switcher battlecard must identify which categories apply to the target incumbent:

### Class A: Data Egress & Storage Inertia
- **Proprietary Encoding:** Data exported in non-standard JSON blobs or obfuscated relational models requiring extensive rehydration.
- **Rate-Limited Egress:** Bulk export endpoints throttled (e.g. 5 requests/sec or strict daily export quotas).
- **Egress Bandwidth Tolls:** High marginal costs charged for moving data out of cloud storage or proprietary clusters.
- **Historical Pruning:** Incumbent imposes data loss penalties or forces deletion upon cancellation.

### Class B: SDK & API Coupling (Blast Radius)
- **High Ingestion Coupling:** Proprietary client-side SDKs embedded across dozens of microservices, web apps, or mobile clients.
- **Proprietary Query Language:** User queries, dashboards, and alerting rules locked in vendor-specific syntax (e.g. proprietary DSLs instead of standard SQL).
- **Webhook & Pipeline Inflexibility:** Inability to multiplex webhook streams or forward raw events to multiple destinations simultaneously.

### Class C: Workflow & Cognitive Friction
- **Team Retraining Overhead:** Custom analyst workflows and dashboard configurations built up over years.
- **Permission & Governance Entrenchment:** Complex RBAC models, audit logs, and directory synchronizations that enterprise IT resists recreating.
- **Organizational Inertia:** "Nobody gets fired for buying the incumbent." Risk aversion from middle management.

### Class D: Commercial & Economic Penalties
- **Seat-Tax Multiplication:** Charging per viewer or collaborator rather than for compute/usage.
- **Overage Tier Cliffs:** Steep multiplier rates (2x-5x) applied as soon as a tier ceiling is crossed by a single percent.
- **Multi-Year Minimum Commitments:** True-up clauses and renewal traps that penalize downsizing.

---

## 2. Switching Complexity Tiers

| Tier | Characteristics | Typical Buyer Friction | Key Objection to Address |
| :--- | :--- | :--- | :--- |
| **Low** | Stateless API replacement, simple SDK swap, no historical state required | Minimal | "Does the new API provide equivalent latency and reliability?" |
| **Medium** | Event streaming, client SDK migration, historical window backfill (30-90 days) | Moderate | "How much developer time is required to swap client instrumentation?" |
| **High** | Stateful datastore, terabyte/petabyte scale, multi-region compliance, complex RBAC | High | "How do we ensure zero data loss during the transition window?" |

---

## 3. Friction Neutralization Principles

When drafting competitive positioning, systematically map our advantages against the active lock-in classes:
1. **Neutralize Class A (Data):** Emphasize open formats, direct SQL/warehouse access, or automated ingestion adapters.
2. **Neutralize Class B (APIs):** Highlight drop-in API compatibility or standardized open protocols (e.g. OpenTelemetry, S3-compatible, SQL).
3. **Neutralize Class C (Workflow):** Show ergonomic parity in core workflows and self-serve team onboarding.
4. **Neutralize Class D (Commercial):** Expose pricing transparency, predictable usage limits, and absence of punitive seat taxes.
