---
name: review-language-gap
description: Mine public customer language, compare it with a company's site messaging, and identify one evidence-backed message change.
---

# Review language gap

## What this workflow is trying to produce

Produce one useful marketing decision:

> "Buyers repeatedly describe the problem/value this way, but the site does not make that
> idea easy to see. Test this specific message in this specific place."

This is a message-gap workflow, not a competitor summary, SEO keyword report, or generic
copywriting exercise.

## 1. Establish the comparison

Read the supplied site URL. Identify:
- the homepage headline and subheadline;
- the main call to action;
- the clearest product/value proposition;
- pricing or plan language if public;
- one or two pages that explain the product in more detail.

Use the buyer context to decide what counts as relevant language. If the site cannot be
read, stop the comparison and report the limitation.

## 2. Build a small evidence set

Research public customer language around the product category. Prefer:
1. G2, Capterra, Trustpilot, app-store reviews, or other public review pages;
2. public Reddit discussions when they contain first-hand product/category experience;
3. public community discussions or comparison pages as a fallback.

Use up to 3 competitor/alternative products and up to 3 useful public sources per competitor.
Do not create fake reviews or treat a marketing claim as customer evidence.

Look for repeated language around:
- the moment that caused someone to look for a solution;
- the outcome they cared about;
- the objection or frustration they had before switching;
- the reason they preferred one solution over another.

Capture short phrases only when useful for traceability. Prefer paraphrase and keep any direct
quote to a few words. Record the source URL and what it actually supports.

## 3. Cluster the language

Group the evidence into 3-5 recurring themes. For each theme record:
- buyer wording or close paraphrase;
- number of independent sources supporting it;
- source links;
- whether it is a pain, desired outcome, objection, or proof point.

Do not count multiple pages that repeat the same review as independent evidence.

## 4. Compare against the site

For each theme, check whether the company's site:
- says it clearly;
- says something related but vague;
- only mentions a feature without the buyer outcome; or
- does not address it.

Pay special attention to language that is specific enough to change the headline, CTA,
proof section, pricing explanation, or first product paragraph.

## 5. Select one gap

Choose the single gap with the strongest combination of:
- repeated buyer evidence;
- relevance to the stated buyer;
- clear absence or vagueness on the site;
- ability to test with one bounded messaging change.

Do not create a numeric score or pretend the evidence proves conversion impact.

## 6. Write the output

Write `reports/REVIEW_LANGUAGE_GAP.md` with exactly these sections:

# Review language gap
## Decision
One sentence naming the message gap and the proposed test location.

## Buyer evidence
A compact table with Theme, Evidence, Sources, and Confidence (high/medium/low).
Confidence must reflect evidence quality, not a predicted business outcome.

## Current site
Quote or paraphrase the relevant current site message and give its URL.

## Gap
Explain the mismatch in 2-4 sentences.

## Message to test
Give:
- Proposed headline or section copy
- Supporting sentence
- CTA if the change affects the CTA
- Exact page/section to change

## Why this is the next test
Explain why this is the most actionable gap from the evidence, without claiming it will
increase conversions.

## Sources
List every public source used with its URL and a one-line description of what it supports.

## Limits
State missing review data, inaccessible pages, duplicated sources, or other uncertainty.

The output is advisory and reviewable. Do not make changes outside the declared report.
