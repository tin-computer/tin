---
name: content-decay-refresh
description: Identify stale marketing content, verify the underlying changes, and turn them into a prioritized refresh brief.
---

# Content Decay Refresh

## Objective

Find customer-facing content that may no longer accurately represent the company's current product, positioning, features, pricing, integrations, or other acquisition-relevant information.

Turn verified findings into a practical refresh queue that a founder or marketer can act on.

This workflow is advisory only. It must not publish, delete, or modify company content.

## Workflow

### 1. Discover the content surface

Inspect the connected company's available website, documentation, repository, changelog, product information, and other relevant company sources.

Identify acquisition-relevant content such as:

- Landing pages
- Product and feature pages
- Comparison pages
- Integration pages
- Pricing-related pages
- Blog posts
- Help or documentation pages that influence acquisition

Prefer pages that directly affect discovery, evaluation, or conversion.

Record the page or content location for each candidate.

### 2. Detect concrete decay signals

Look for evidence that a page may be stale, including:

- Features that have changed, been renamed, or been removed
- Outdated product terminology
- Old pricing or plan information
- Deprecated or changed integrations
- Old screenshots, examples, or product flows
- References to previous product behavior
- Outdated statistics, dates, or factual claims
- Broken or changed links
- Old positioning or messaging
- Current capabilities documented elsewhere but missing from important acquisition pages

Do not treat age alone as evidence of decay.

A candidate should have a concrete signal, such as a newer source contradicting the page or a current source showing that important information has changed.

### 3. Verify each candidate

For every potential issue:

1. Locate the exact stale claim, section, link, or asset.
2. Find the strongest available current source that can confirm or contradict it.
3. Compare the old content with the current evidence.
4. Record the evidence and explain what changed.
5. If the evidence is incomplete or conflicting, mark the finding as unverified.

Prefer evidence in this order:

1. Current product or company documentation
2. Current website or product pages
3. Changelog or release notes
4. Current source code or configuration when directly relevant
5. Other connected company sources

When sources conflict, do not silently choose one. Note the conflict and identify what additional evidence would resolve it.

### 4. Assess acquisition impact

For each verified finding, assess impact using the content's role and the nature of the change.

Use:

- **High** — affects a core acquisition page, pricing, a major feature, product capability, or information likely to materially change a prospect's understanding
- **Medium** — affects useful acquisition content but is unlikely to block or substantially change evaluation
- **Low** — minor wording, cosmetic, or low-impact factual issue

Do not invent traffic, conversion, revenue, search-volume, or customer-impact numbers.

If impact is uncertain, explain why instead of inventing a score.

### 5. Recommend the smallest useful update

For each verified finding, recommend a specific update.

Prefer actionable recommendations such as:

- Replace an outdated feature description
- Update a pricing reference
- Remove a deprecated integration
- Replace an obsolete screenshot
- Update a product example
- Add a newly documented capability to a relevant page
- Fix a changed destination link

Do not rewrite the entire page unless the evidence indicates that a broader update is necessary.

### 6. Produce the refresh brief

Create a concise Markdown report with exactly these sections:

## Executive summary

Summarize the main decay patterns and the number of verified, high-impact findings.

## Priority queue

For each finding include:

- **Priority**
- **Page or content location**
- **Stale claim or issue**
- **Current verified information**
- **Evidence/source**
- **Recommended update**
- **Acquisition rationale**

Order findings by priority, then by likely acquisition importance.

## Quick wins

List verified changes that appear straightforward to implement and useful to address promptly.

## Unverified items

List potential issues where the available evidence was insufficient or conflicting.

For each item, state what evidence is missing or conflicting.

## Scope and limitations

State:

- Which company sources were available
- Which content surfaces were inspected
- Any sources that were unavailable
- Any important evidence or attribution limitations

## Safety and quality rules

- Never publish, delete, or modify content.
- Never fabricate current product information.
- Never invent traffic, conversion, revenue, search-volume, or customer-impact metrics.
- Never present an unverified claim as fact.
- Preserve links to the evidence used for important findings when available.
- Clearly distinguish verified facts from recommendations.
- Do not treat content age alone as evidence of decay.
- Prefer specific evidence over generic suggestions.
- Keep recommendations proportional to the evidence.
