# Add `growth.pricing_friction_test`

## What this workflow does

`growth.pricing_friction_test` turns a founder's existing first-party product analytics into one concrete pricing or packaging experiment.

It reads the current pricing page and project context, discovers the actual PostHog event definitions, builds a bounded pricing funnel, identifies the strongest observed drop-off, and produces **exactly one** evidence-backed experiment.

The output is:

`reports/PRICING_FRICTION_TEST.md`

It contains:
- the actual events used to measure the pricing funnel;
- observed stage conversion and limitations;
- the strongest friction signal;
- one experiment with a control, one changed variable, target population, primary metric, guardrails and rollback condition;
- instrumentation gaps when the evidence is insufficient.

The workflow never changes pricing, creates an experiment, modifies analytics, contacts customers or publishes anything.

## Who would run it

A founder or growth engineer who already has PostHog connected and wants to answer:

> "What is the next pricing-page test I can justify from our own behavior data?"

## Why this is missing today

Tin already has:
- `product.analytics_brief`, which produces a broad recurring analytics report across activation, event trends, traffic, errors and supported breakdowns;
- `competitor.watch`, which watches competitors' public pricing and changelogs;
- landing-page and cold-read workflows that inspect messaging.

None of those workflows specifically closes the loop from **pricing-page behavior → one bounded pricing/packaging experiment**.

The distinction matters: this workflow does not summarize the product funnel or monitor competitors. Its output is a reviewable experiment specification grounded in the company's own pricing-page conversion evidence.

## How it works

1. Read onboarding and Feature map context.
2. Read the current public pricing page.
3. Discover the project's real PostHog pricing, checkout and conversion events and properties.
4. Query an ordered, bounded funnel over the requested window.
5. Identify the strongest measured friction point without claiming causality.
6. Produce one experiment with explicit control and guardrails.
7. If the instrumentation cannot support the decision, produce an instrumentation-gap report instead.

## Why this idea

PostHog's funnel documentation describes funnels as a way to identify where users are getting stuck and to compare relative conversion between steps. Its product-analytics documentation also positions funnels as a tool for growth engineers to find conversion leaks. Stripe's pricing-experiment guidance recommends isolating the variable being tested, defining metrics in advance, and considering both conversion and longer-term business effects.

References:
- https://posthog.com/docs/product-analytics/funnels
- https://posthog.com/docs/product-analytics
- https://stripe.com/resources/more/pricing-experiments

## Testing

Added `tests/test_pricing_friction_test_package.py` with offline checks for:
- package identity and executor;
- bounded inputs;
- required PostHog read-only capabilities;
- service call/response bounds;
- declared prompt/skill resources;
- single markdown output contract.

The test does not contact PostHog or run a live model.

Recommended checks before merge:

```bash
uv sync --frozen
uv run tin-lite validate-community
uv run pytest tests/test_pricing_friction_test_package.py
```

## Registration

This PR intentionally does not modify `PUBLIC_WORKFLOWS`. Public Registry registration is maintainer-controlled according to the repository's workflow contribution guide.

## Notes

The workflow is deliberately conservative: if event identity, plan properties or conversion events are missing, it reports the instrumentation gap instead of inventing a funnel or recommending a pricing change.
