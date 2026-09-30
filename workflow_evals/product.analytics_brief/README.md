# Analytics qualification

The versioned cases in `qualification.json` share the normal package qualification
contract. They are test specifications, not evidence that an agent passed them.
`tests/fixtures/product_analytics/{website,accounts}.json` contains fabricated events,
documented identity/event semantics, and independently specified ordered populations.
The website population is 6 → 3 → 2 → 2; the account population is 6 → 3 → 2.
Duplicates, tied timestamps, later-only actors and cross-attempt/cross-actor events
must not inflate those counts. The website has real synthetic pageviews and named
path/source/device buckets; the account dataset has no pageviews.

Run `uv run pytest tests/test_product_analytics.py` and the package qualification
CLI offline. Tests execute only the fixed reviewed resources, never incoming PR
code with credentials. Statistical fixtures include an independently published
Fisher example, symmetric/empty tables, Holm correction and small-sample rejection.
Plausible but inconsistent provider tables are negative cases.

For an authorized live case, use an isolated PostHog test project with the matching
fixture events and metadata, and select it in the Tin project's analytics.posthog
connection (the web and backend cases need separate Tin projects or a reselection
between runs; the package never names a PostHog project). Do not seed a customer's analytics with fixtures.
Name the Tin project, authorized payer and spending ceiling before starting through
the existing evaluator. Retain source/case digests, usage receipts, report, query
outcomes and human rubric judgments. A mocked response or deterministic provider
SQL probe does not establish agent quality or expected model cost.

The `repeat_labels` case supplies the previous report as project context. Negative
provider cases use a controlled response fixture: a permission_denied refusal from
Tin's gateway (see tests/connection_fakes.py), or a monotone but wrong
funnel count. These are offline response/quality tests, not permission to alter a
real provider's response. Normal output assertions require `Status: complete`;
headings or successful artifact delivery alone cannot pass.

`team_exclusions` exercises the structured `exclude_email_domains` and
`internal_flag_property` inputs; the report may name the fields and operators, never the
domain. `unsupported_exclusions` uses a contains-pattern, which no rule supports.
`signup_identity` uses the `identified` events in `provider_edges.json`: anonymous
pageviews, then a server sign-up under the identified distinct ID with the same person_id
and no `$session_id`. `zero_inventory` expects a failed run: Tin's analytics-brief.v1 check
refuses to publish a brief that measured nothing and keeps its diagnostic on the run.

Provider SQL probes can substitute an inline fabricated `fixture` CTE for `events`
and execute through an authorized read-only connection, without ingestion. Such a
probe must itself pass Tin's HogQL guard (one SELECT, final LIMIT <= 1000, no UNION
or OFFSET, at most 8000 bytes); build fixture rows with arrayJoin, not UNION ALL.
The coverage and dimensions queries were restructured for that guard and their
recorded responses await live requalification. Their
sanitized columns/results can be retained as regression fixtures. The reusable-v2
builders (2026-09-29) changed the coverage, funnel, traffic and dimensions text and added
the `input_exclusions`, `channels`, `identified` and `identified_sessions` cases. Those
responses were produced by running the exact generated SQL on embedded ClickHouse (chdb)
over the listed events; where a query's columns did not change they matched the recorded
PostHog rows. They still need one live pass through PostHog HogQL. Query compilation
and numerical provider compatibility are distinct from a full agent report and
from scheduled execution after deployment.

The large_catalog case adds 201 independently named one-row events to the four-event
website fixture. Bounded discovery must disclose 200/205 types without blocking exact
selected-event analysis or asserting absence of omitted events. A response that returns
fewer rows than min(total_event_types, 200) is still an incomplete query and must fail.
Web cases should exercise the documented PostHog defaults even when event descriptions
are empty. Missing product session keys must not suppress independently valid traffic
analysis or become a fabricated product conversion rate.
