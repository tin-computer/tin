# Query patterns

These are patterns, not fixed event names. Replace every event/property name only after confirming it exists in the connected PostHog project.

## Event discovery

Use:
- `event_definitions.list` with a pricing-related search such as `pricing`, `checkout`, `subscription`, or `signup`.
- `property_definitions.list` for the confirmed event names.

## Funnel shape

Prefer a single ordered funnel with no more than four stages:

1. pricing page entry;
2. plan/pricing action;
3. checkout or trial start;
4. paid conversion.

The query should return aggregate counts by stage and, when supported by actual properties, by plan or billing interval.

## Safety checks

- One SELECT or WITH ... SELECT per query.
- Explicit UTC start-inclusive/end-exclusive time bounds.
- A bounded LIMIT.
- No raw person rows.
- No email, session payload or unnecessary user-level export.
- Check `has_more` and `truncated`.
- Do not infer missing stages from unrelated event totals.
- Do not treat a provider error or empty result as zero.

## Interpretation

Use relative conversion to identify the stage with the largest observed loss, but do not turn a correlation into a causal claim. If plan-level data are sparse or absent, report that limitation and avoid a plan-specific recommendation.
