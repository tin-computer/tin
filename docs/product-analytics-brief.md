# Product analytics brief

`product.analytics_brief` reads one connected PostHog project and writes a standing
brief: activation, key-event trends, traffic, error signals and one useful breakdown.
It reports findings and their evidence, without recommendations or changes to the
customer's product. It does not inspect session replays or fix instrumentation.

## Set it up once

Connect PostHog in the project's Integrations: OAuth with read scopes only, then
choose the one PostHog project the brief reads (see
[Stripe and PostHog connections](stripe-and-posthog-connections.md)). The package
binds `analytics.posthog` with `query.read` and `definitions.read`; tokens stay in
Tin, never in workflow files, inputs or the sandbox. The brief has no project input:
it always reads the project selected on the connection, so switching that project
changes what the next brief reads (its schema check then reports the change).

Save the workflow with the reporting window and any known funnel or exclusions. Leave the funnel empty to have the procedure propose one
from available event evidence and project context. Discovery reads up to 200 event types
ranked by recent volume and discloses the total catalog size. Selected metrics query their
complete event/window populations; events outside the discovery list are not assumed absent.
The report states what it chose
and why; edit the saved inputs to correct it. Missing semantics are a limitation,
not permission to invent an activation event or treat an identifier as a human.

Two optional inputs remove your own team without relying on the procedure to read prose:
`exclude_email_domains` (up to ten lowercase domains, such as `example.com`; a person whose
current email ends in `@example.com` or `.example.com`, in any case, is excluded) and
`internal_flag_property` (one event property, or `person:<property>` for a person property;
rows where it is `true`, `"true"` or `1` are excluded). Tin compiles both into the queries
itself. The prose `exclusions` field still takes up to six more rules: exact values, domain
suffixes or truthy flags. Anything else, such as "contains" or a pattern, makes the brief
report unsupported exclusions instead of guessing. Rows missing the field stay included and
are counted.

Choose manual, daily or weekly execution through the ordinary saved-workflow form.
Existing schedule authorization and billing rules apply. Results arrive in Tin's
Files and Activity, with a separate `reports/analytics/<run_id>.md` for each run.
This package does not add email or Slack delivery.

Use a separate Tin project for a separate PostHog project. Website visitors and
product accounts are different populations; this workflow does not join them.
A project without pageviews can still produce a useful product brief, with traffic
explicitly unavailable. When pageviews exist, the procedure checks PostHog's documented web
properties through the same bounded queries.

A web funnel that runs from pageviews to a sign-up the backend records follows people, not
browser sessions: server events carry no `$session_id`, and `identify()` gives the signed-up
person a new distinct ID. By default such a funnel uses PostHog's `person_id`, which links the
anonymous and identified IDs, and counts a later step when the same person does it within 24
hours of a step-1 event (`window:24`). Browser sessions remain the fallback when a project
never identifies people. If the chosen identity cannot link the steps (people did the final
step, others started the funnel, and none joined), the brief says so and withholds the funnel
instead of showing 0%.

Traffic is grouped into fixed channels that Tin assigns, not the procedure: UTM medium first
(Paid, Email, Social), then the referring domain, lowercased and without `www.`: `$direct` or
empty is Direct, your own host or `website_hosts` is Internal, then AI assistants (ChatGPT,
Perplexity, Claude, Gemini, Copilot), Search, Social, and Referral for everything else. Inside
Referral the eight domains with the most entry sessions are named and the rest are Other.

## What is checked

The bounded procedure chooses and explains the analysis. Declared Python/SQL
resources own query construction, ordered counts, rates and statistical checks.
Queries return aggregates, with raw identities kept inside PostHog. Source data
and previous reports are evidence, never instructions. Read-only access comes from
the OAuth scopes. Tin's gateway checks each query's shape (one SELECT of at most
8000 bytes, final LIMIT of at most 1000, no OFFSET or UNION) but not its analytic
semantics; the procedure instructions are reviewed behavior, not a SQL sandbox.
Each run uses at most eight calls: seven aggregate queries and one property-definition
read that checks the chosen identity and dimension properties are strings.

Every table identifies its window, population, exclusions and unit. Queries use
explicit UTC boundaries. Missing keys, late instrumentation, zero denominators,
small samples and incomplete provider responses stay visible. First observed data
does not establish when an event became reliable or what its firing site means.
A failed required query makes the brief incomplete; an honest diagnostic is not
a passing ordinary qualification case.

Before publishing, Tin reads the report's `Status:` line and its evidence block
(`analytics-brief.v1`). A brief that measured nothing fails its run with a plain reason: PostHog
returned no events in the 90-day lookback, every query was refused or invalid, or none of the
selected events occurred in either window. The diagnostic stays readable on the failed run and
is not added to Files. Any other incomplete brief is published, and the run summary reads
"Analytics brief incomplete: <reason>".

A screened breakdown is descriptive evidence, not proof of causation. The report
names the test and its multiple-comparison correction. No supported split is a
valid outcome; the procedure must not keep searching until it finds significance.

## Qualification and publication

The package and `workflow_evals/product.analytics_brief/qualification.json` use the
same creator/qualifier contract as external contributions. Static checks establish
shape. Offline tests exercise calculations, query construction and bad responses.
They do not establish provider compatibility or model quality.

Live qualification separately checks the exact generated SQL against controlled
provider cases and reviews real reports against retained query results. Package
and case digests identify the evaluated version. A private on-demand copy is a
different manifest from the scheduled public package; do not describe its run as
exact public-package acceptance. Public schedule execution needs its own test
deployment or post-merge acceptance.

Model cost is unmeasured until priced runs exist for that package and case. A
configured ceiling is not an expected price. The existing usage receipts and
ledger charge verified actual model usage; connected PostHog spending is separate
and is not asserted to be free. Event/schema complexity, discovery, response sizes
and interpretation affect agent cost.

Maintainers review source, useful results, safety and measured costs before
publication. Explicit `PUBLIC_WORKFLOWS` registration is reviewed in the PR;
deployment/catalog sync is the publication step. No creator or evaluator publishes
automatically, and existing private runs and saved versions stay pinned.
