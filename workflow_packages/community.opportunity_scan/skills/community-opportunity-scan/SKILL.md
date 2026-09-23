---
name: community-opportunity-scan
description: Identify public community conversations where a business can participate helpfully, while checking each community's current participation rules.
---

## Objective

Produce a small, evidence-backed list of public community opportunities that match the project's target audience and problem. The useful unit is a conversation or clearly identified discussion opportunity, not a generic list of communities.

## 1. Establish the business context

Read relevant project state first. Identify, only from available evidence:
- product and category
- target audience / buyer
- problem being solved
- relevant use cases and terminology
- geography or language constraints, if explicitly documented

Use the `focus` input to narrow the scan when supplied. Do not invent an ICP, product capability, customer segment, or claim about the business.

## 2. Search public discussions

Use available public web/community search and look for concrete conversations where the target audience is discussing the identified problem. Useful sources can include Reddit, Hacker News, GitHub Discussions/issues, public forums, and other public communities relevant to the project.

Prefer:
- a specific thread or discussion over a homepage
- a recent conversation over an old one when both are otherwise comparable
- first-party community pages and the actual discussion URL
- conversations showing a real question, pain point, request, comparison, implementation problem, or request for recommendations

Do not treat search-result snippets alone as evidence when the underlying page can be inspected.

## 3. Verify participation rules

For each shortlisted community, inspect the community's current rules, FAQ, posting policy, or equivalent first-party guidance when available. Record concrete restrictions relevant to participation, such as self-promotion limits, link restrictions, required disclosure, designated promotion threads, or prohibitions on unsolicited sales.

If rules cannot be verified, label them as unverified. Never assume that a community permits promotion merely because related posts exist.

## 4. Select opportunities without hidden scoring

Keep the shortlist bounded (normally 5-10 opportunities). Select based on explicit evidence of relevance and participation eligibility. Do not invent a numeric score or claim that one community is universally best.

Exclude:
- conversations that are clearly unrelated to the product/problem
- communities whose rules clearly prohibit the proposed participation
- spam, engagement-bait, or requests to evade moderation
- opportunities that require pretending to be a customer or hiding a commercial relationship

A useful response angle must be genuinely helpful to the conversation. If mentioning the product would violate the rules or be inappropriate, say `do not pitch` and describe the useful non-promotional contribution instead.

## 5. Write the artifact

Write `reports/COMMUNITY_OPPORTUNITY_SCAN.md` with these sections:

# Community opportunity scan

## Business context
- concise evidence-based summary of the product, audience, and problem used for the scan

## Opportunity table
For each opportunity include:
- Community
- Conversation / topic
- Why it matches the target problem
- Conversation date (or `not stated`)
- Participation-rule evidence
- Recommended participation mode: `helpful reply`, `answer/question`, `resource contribution`, `observe only`, or `do not pitch`
- Helpful response angle, limited to claims supported by the project context and the conversation
- Conversation URL
- Rules URL

## Rules and caveats
Document any communities whose rules could not be verified, stale-looking pages, ambiguous promotion policies, or other limitations.

## Next actions
Give a short ordered action list for a human reviewer. The first action for each opportunity should be review/verification, not automatic posting.

## Research notes
State the search date, public sources inspected, and important gaps. Distinguish observed facts from interpretation.

## Quality constraints

- Never fabricate a thread, comment, user, rule, date, quote, or URL.
- Do not recommend evading moderation, hiding sponsorship, or disguising marketing as independent advice.
- Never auto-post or send outreach from this workflow.
- Do not copy long passages from source pages; summarize with links.
- If a source is inaccessible, mark the fact as unverified rather than filling the gap from memory.
