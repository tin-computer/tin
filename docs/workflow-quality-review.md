# Make workflow results useful and verifiable

The ClawMessenger trial produced useful artifacts but exposed gaps between execution, evidence and editorial decisions. Scheduled jobs could fail before a visible run existed. The organic audit counted unavailable checks as completed. Planning and drafting could advance overlapping topics without a clear buyer, position or argument. Expensive research could spend much of its budget processing its own accumulated context.

This change improves the existing workflows and their evidence contracts. It does not deploy Tin, repair a customer website, publish generated articles, add another workflow engine or introduce a shared “partial success” run state.

## Review context

The founder reviewed the September 21 trial and asked why September 15–21 appeared quiet, why successful runs contained incomplete results, why keyword research and AI prompts were narrow, why content failed to establish a persuasive position, why planned topics duplicated existing pages, why the weekly brief ignored the broader direction, why a brand-character workflow ran and why research/articles cost much more than a brief.

Subsequent discussion established these decisions:

- Keep onboarding smooth. Resolve routine missing evidence within the workflow and ask only questions that change a necessary decision. Do not add a long questionnaire or expose repeated internal failures.
- Improve the existing workflows; do not create overlapping replacements. Check relevant new integrations before auditing or briefing, but do not confuse connection availability with measured evidence.
- Distinguish successful empty keyword measurement from a failed or skipped lookup. The former supports conservative low/unproven traffic expectations; the latter does not support a demand conclusion. Do not invent search volumes.
- Take positioning from the project's brand and positioning files; content planning and drafts follow it and never narrow it. Price can matter, but it is not a universal position. Answer pages should reinforce a supportable argument and verify competitor facts.
- Hide `content.public_article` from public discovery and onboarding recommendations while preserving direct execution, saved configurations, schedules and history.
- Test Product analytics with the newly authorized PostHog GET/POST access. Keep the added-evidence comparison separate from same-evidence prompt comparisons.
- Preserve useful memory behavior, explain costs by cause, save partial research early, and defer a proposed workflow/model-optimization field.
- Include the observations, source types, user decisions, tests and remaining acceptance gaps in the PR. Produce a review pack alongside the original artifacts.

## Evidence that changed the diagnosis

The initial six-day explanation was incomplete. A scheduled answer draft ran September 15 and entered review, then its parent deadline ended the execution. PR #25 repaired that separately. September 17 article and September 18 site-health occurrences fired but failed billing admission before creating a run row. The configured schedules remained active with stale next dates and no useful error. No new runs appeared September 16–20, although not every day had scheduled work. Schedule creation delays do not explain that period.

The audit's 55 retained pages did not establish that every technical check applied. DataForSEO conditions certain metadata flags on canonical context. The old normalizer dropped that context. A local reproduction confirmed that an unavailable check could count as completed; it did not establish missing metadata on the customer's site.

The keyword inventory was broad in count but narrow in useful buyer opportunities: most candidates addressed the wrong buyer. Some unsupported seed proposals lacked measurements. More names would not solve that; seed selection, fit, measurement interpretation and coverage need to work together. The candidate retains the deterministic provider limits and ranking schema while changing those judgment instructions.

The two public articles addressed REST replies and restart recovery. They were answerable implementation questions, but the trial did not establish separate acquisition gaps. A drafting contract could succeed without demonstrating why another page was worth writing.

Usage receipts explain much of the cost difference. The reviewed second article used eight Astra requests; approximately 85% of its model charge came from input and cache processing. Two failed research runs spent approximately 47% and 39% of their charge on context compaction. A subsequent reservation could exceed the remaining ceiling even before the next request ran. Recovery bugs and context cost are separate problems.

The brand-character artifact came from a trial extra, not an established founder design request. The workflow was introduced in the earlier catalog by Ege Demir (tin-lite commit cdda6bf, September 8). This change constrains the recommendation through its description; it does not add a new authorization system.

## Sources

The investigation used these sources rather than treating the sampled HTML report as complete evidence:

- Saved trial Markdown, run records and charge records under the local `tin-lite-tests/mcp-trial/runs/2026-09-21/artifacts/claw-messenger` archive.
- Read-only run, schedule, effect and usage evidence; actual execution dates were distinguished from simulated trial labels.
- Current and pinned Tin definitions, activity code, procedure skills, architecture documentation and the existing fixes listed below.
- The founder's corrections in this conversation, including customer hardware requirements, onboarding preferences, positioning, integrations and public-workflow visibility.
- `tin-artifacts/skillmine-workflow-matches/report.md`: adapted evidence, intent, argument, evaluation and cost-control mechanisms to the existing contracts. No third-party implementation was copied.
- Think Like an Agent's newer intent-to-natural-question process, rather than its older static prompt bank.
- [DataForSEO On-Page documentation](https://docs.dataforseo.com/v3/on_page-pages/) for applicability conditions; first-party [ClawMessenger](https://www.clawmessenger.com/docs), [Photon](https://photon.codes/pricing) and [Sendblue](https://www.sendblue.com/) pages for dated product claims.
- Authorized, bounded PostHog aggregate queries. Private raw provider responses, customer identities, full internal inventories, credentials and internal acceptance logs are excluded from this repository.

The [artifact review and comparison pack](https://tin-artifacts.vercel.app/tin-mcp-trial/clawmessenger/2026-09-21/workflow-improvements) contains the reviewable examples and aggregate results. Original artifacts remain unchanged apart from navigation to the new note.

## Implementation

### Evidence and audit

`organic-audit-v9` preserves canonical/sitemap applicability, distinguishes problem/pass/not-applicable/unknown, exposes evidence completeness and retains broken resources for HTTP checks. It enables sitemap use and adds a bounded 28-day matching-property Search Console page sample through the existing integration gateway and effect receipts. GSC rows establish observed search performance, not comprehensive indexing or absence.

Older audit policies keep their former normalization and schema. New finding schema 2 is validated by the technical-fix source reader; this compatibility adjustment adds no repairs. Completion accepts the pinned compatible policy, preserves successful answers and records missing context as unknown. Historical normalized artifacts cannot recover discarded provider context.

Keywords use v5 instructions and broader supported buyer-job seeds; explicit older pins remain valid. Content-plan v5 requests buyer/decision/alternatives/advantage/proof, a structured argument and an actual coverage gap. It increases the excerpt limit for already fetched pages without adding an unbounded crawl. New keyword definitions prefer connected matching-property GSC evidence; previous explicit settings remain pinned.

### Writing and briefing

Answer pages emit a validated six-field argument in an internal metadata block. The renderer removes it from article copy and saves it in evidence. Planned-content procedures check overlap early, save their argument in generation notes and verify current pricing/hardware claims. The quantity requested by a plan is a maximum; a supported “already covered” decision is useful output.

Visibility generation validates an intent/subquestion bank, selects its five measurement questions from that bank and includes the bank in the frozen panel hash. Brand checks remain separate. The report says “Without web search.” Existing definitions keep the old panel contract.

Weekly briefs prioritize bounded analytics reports, current integration metadata and up to three bounded founder-context files from one immutable project snapshot. Scans and audit/briefing procedures also consider relevant integration evidence. Availability metadata contains provider and status only. Native reporters do not silently launch paid child workflows to fill missing measurements.

Native answer, visibility, brief, memory and scan instructions are embedded in immutable workflow definitions. Definitions without the new field use archived pre-change suites. This avoids changing the model contract underneath saved runs. Existing procedure resource pins continue to govern agentic skills.

### Analytics, schedules and cost

The existing Product analytics package adds a validated host allowlist, carried in its query binding and applied consistently to standard pageview inventory, coverage and traffic. The effective host list comes from project context when available. Custom events retain their stated project-wide scope; the package does not claim that a browser host filter scopes server events.

Native schedule billing admission errors use the existing pause/error/activity mechanism; retries repair the Temporal pause if necessary. No budgets are raised, fabricated runs created or financial history repriced.

Research instructions save a usable early result, update it progressively and stop when the decision is supported. Usage exposes bounded stage labels and inclusive stage totals from trusted receipts, including context compaction. Missing prices remain unknown. Public-article discovery filters are shared by API, MCP and onboarding, while execution/history remain intact.

## What ran

- Broad local suite: **1,955 passed, 634 skipped**. The skipped database acceptance tests need a disposable test database; no production database was used for tests.
- Temporal scheduled-review suite: **3 passed, 2 skipped**, run separately with network access for the test server.
- Ruff, import-boundary checks and all four community package validators passed.
- Focused tests cover provider-context applicability, legacy semantics, broken resources, GSC validation, structured arguments, neutral/frozen AI questions, secret-free integration inventory, schedule billing blocks, native instruction pins, discovery filtering, analytics host binding and usage-stage sanitization.
- The existing live Product analytics workflow completed under a $5 ceiling for **$2.32**. It correctly withheld unsupported funnel/payment interpretation and exposed the project-wide scope limit.
- Three candidate host-scoped PostHog requests passed the package's inventory, coverage, reconciliation and traffic validators. Candidate package code was not deployed.
- A focused live research run completed for **$1.13** under a $5 ceiling and saved an early result before updating it. Candidate research constraints were supplied as inputs to the existing deployed workflow; this is not proof of a deployed new version or a same-question savings benchmark.
- Same-model, same-frozen-evidence prompt pairs cover two answers, content planning, weekly briefing and research synthesis. Additional PostHog variants explicitly change the evidence. Structured AI panel pairs passed the production validators; the candidate bank contained 18 questions and selected five.
- The audit was recalculated locally against retained evidence. The keyword editorial smoke test was not valid full-inventory ranking acceptance and is not presented as such. After explicit user approval, the structured saved-inventory comparison completed through the existing OpenAI service. Both v4/v5 outputs passed production validation: the same 30 eligible candidates were reviewed and all 208 saved candidates were assigned exactly once after restoring screening exclusions. The candidate produced five groups versus nine, but still offered little demand reasoning and recommended creating pages without establishing gaps. This is schema acceptance, not proof of better ranking or broader discovery. See the comparison pack for both outputs.

The comparison caught remaining weaknesses: formal AI questions, a still-narrow integration content plan, excess process detail in the brief, a mistaken event label and an omitted Photon AutoScale pricing condition. The final instructions clarify exact event names, source separation and pricing footnotes. Those final prompt refinements were not model-rerun; the original comparison outputs preserve their defects.

## Existing fixes and acceptance gaps

| Earlier work | Status supplied and verified during review | What this PR does |
|---|---|---|
| #31 visibility domain validation/retry guard | Live success and no extra replay charge | Preserve recovery tests; improve questions and labeling separately |
| #20 onboarding publisher | Native plan published; full onboarding acceptance open | Preserve publisher; keep onboarding brief |
| #28 research recovery | Live success, durable checkpoint, replay without extra charges | Preserve recovery; reduce context growth and improve early output |
| Code-map recovery | Live success and replay without extra calls/charges | No redesign |
| #29 bundle limits | Shared path passed; full site-health PR acceptance open | No expansion |
| #25 scheduled-review lifetime | Original draft recovered to an active approval wait | Preserve deadline fix; address separate billing admission visibility |
| #32 finding lookup | Content findings recognized; missing IDs rejected before charge | Support new audit schema; positive repair-to-PR still unverified |

## Rollout and review

CI passed on implementation commit c73035f: 2,550 tests passed, 44 skipped, plus browser, lint, import-boundary and package checks. Review the examples for accuracy, buyer relevance, argument and overlap. Deploy only after approval and activate new immutable definitions through the normal registry path. Existing saved configurations retain their pins until deliberately updated.

After deployment, accept the new contracts with bounded live runs: an organic audit using sitemap and matching GSC evidence, a broadened keyword discovery/ranking run, a structured content-plan run and a brief consuming the saved host-scoped analytics report. Do not treat a single prompt pair as a statistical quality result.

The audit's next reliability milestone remains an independently reviewed fixture site containing missing metadata, broken links, redirect chains, tracking canonicals and intentional indexing exclusions. Track false alarms and missed defects separately. Selective JavaScript rendering and further repairs come after that evidence; this PR does not claim to implement them.

Possible regressions are bounded by versioned policies, immutable instruction suites, validation and old-definition fixtures. More context can increase cost, so excerpts remain bounded and saved reports precede new research. Host scoping can exclude pageviews without `$host`; reports must state their scope. Unknown audit outcomes can make new reports appear less complete than old ones because they describe the evidence more accurately.
