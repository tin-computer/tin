# Case note: `growth.community_signal_scan`

## User and outcome

A small-company founder or marketer runs this on demand to find a short, reviewable set of
recent public conversations where people describe a problem relevant to the company's target
audience. The report supports customer discovery and careful community participation; it does
not promise leads, demand, or sales.

## Why this is distinct

Tin's `research.deep_dive` investigates a project question and produces a decision report;
`organic.keyword_plan` studies search demand and search results; `organic.audit` checks the
company's site and visibility; `outreach.email_shortlist` identifies existing private
relationships; and `content.public_article` drafts an article from project evidence. This
workflow instead finds first-person problem evidence in public community conversations and
assesses each conversation for audience, problem, product, and participation fit.

## How it works

The run reads project context and bounded audience, problem, and product inputs; searches
public web/community sources with concrete queries; verifies candidate conversations and
dates; deduplicates and groups supported patterns; and creates one Markdown artifact with
source links, separated evidence and interpretation, fit assessments, community considerations,
answer-first response material, and a small human-led experiment where justified. It returns a
clear no-op report when it cannot verify a suitable conversation.

## Review and safety boundaries

Public posts can be context-sensitive, undated, inaccessible, or governed by different
community rules. A problem statement does not establish purchase intent or lead qualification.
The output therefore preserves sources, labels uncertainty, and requires human judgment.
The procedure never posts, comments, messages, emails, contacts posters, creates accounts,
collects private contact information, or automatically promotes the product. Where a community
prohibits generated or AI-edited replies, it withholds ready-to-post text.

## Testing performed

The focused tests check required input bounds, optional array bounds, the procedure executor,
the run-ID Markdown artifact path, absence of messaging/posting integrations, the no-action and
no-op instructions, static package loading, and synthetic report outputs through Tin's existing
offline qualification fixture checker. They also exercise Tin's input normalizer against empty
required fields and out-of-range values. No fixture URL is requested.

## Offline qualification case

- **Case name/version:** `community_signal_scan_offline_v1`, qualification schema version `1`.
- **Synthetic inputs:** early-stage SaaS founders; founders struggle to understand why trial
  users abandon onboarding; a fictional SaaS analytics product that identifies onboarding
  drop-off points; Synthetic SaaS Founders Forum; United States; English; 90-day recency; up to
  eight opportunities.
- **Fixtures:** one accessible first-person SaaS founder conversation; one highly relevant
  search snippet whose underlying source cannot be opened; and one accessible gardening post
  with overlapping terms but an unrelated audience and problem. All source URLs use the
  reserved `.invalid` domain and all content is invented.
- **Expected accepted result:** include the useful conversation's source URL, date and first-
  person evidence; assess audience, problem and product fit separately; include review status.
- **Expected unverifiable result:** do not include the snippet as an opportunity; identify the
  inaccessible/unverified source as a limitation.
- **Expected irrelevant result:** exclude the gardening conversation and return a no-op report
  because no suitable conversation remains.
- **Invalid-input expectation:** Tin's input normalizer rejects empty audience, problem, or
  product context, recency outside 1–365, and maximum opportunities outside 1–20. These are
  pytest validation cases rather than qualification-run cases because Tin validates every
  qualification case's inputs before evaluation.
- **What the case proves:** the declared synthetic expected reports satisfy the qualification
  assertions, preserve useful evidence and fit/review fields, and distinguish inaccessible or
  irrelevant candidates from accepted opportunities. The input tests exercise Tin's actual
  schema normalizer.
- **What it does not prove:** the Codex procedure was not executed; the canned report fixtures do
  not measure model decision quality, live-web coverage, actual community rules, marketing
  performance, or cost.

The qualification case is offline and uses synthetic fixtures. It demonstrates expected decision behavior but does not establish live-web research quality or live community-rule coverage.
