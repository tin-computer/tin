Read the project's onboarding plan, Feature map, optional pricing-page URL, reporting window, and focus.

Follow the pricing-friction-test skill. Use only the declared read-only PostHog connection for analytics. Review the current public pricing page when a URL is available from the input or project context.

The output must be exactly:
reports/PRICING_FRICTION_TEST.md

The report should identify:
- the observed pricing/packaging funnel and the events used;
- the strongest evidence-backed friction point;
- what the data does and does not establish;
- exactly one proposed experiment, with hypothesis, single changed variable, control, target segment, primary metric, guardrails, and stop/rollback condition;
- an instrumentation gap if the required events or properties are not available.

Do not change pricing, create an experiment, publish content, modify PostHog, contact customers, or start another workflow. Do not invent event meanings, prices, customer identities, conversion rates, sample sizes, or causal explanations. If the evidence is insufficient for a responsible experiment, produce a diagnostic report instead of guessing.
