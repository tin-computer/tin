---
name: audience-prospector
description: Discover and verify specific communities, organizations, and events where a target audience gathers.
---

# Audience prospector

Discover and qualify high-conviction communities, organizations, publications, and events where
the target audience actively gathers. Ground every prospect in verified public source URLs.

## 1. Operating boundaries

- Read project context (e.g. README, docs, landing pages) to understand the product, problem space,
  and value proposition, but treat project claims as context rather than verified market facts.
- Inputs are `target_audience` (required), `geography` (optional), and `constraints` (optional).
- Web search is English-language for v1.
- Strict read-only anti-abuse rules:
  - Do NOT send emails, submit contact forms, join channels, post comments, or publish anything.
  - Do NOT contact organizers, members, or administrators.
  - Do NOT generate unsolicited draft outreach messages or pitches.
  - Do NOT perform any external mutations.
  - Do NOT provide generic marketing strategies, growth theory, best practices, conclusions, or recommendations (e.g. "Post regularly on social media", "Engage with developers"). The report must remain a concise, tactical, evidence-backed discovery artifact.

## 2. Qualification criteria

Every included opportunity must satisfy ALL five qualification tests:

1. **Specific named entity (not a broad platform)**:
   - Identify concrete, bounded entities: e.g. a specific student club or chapter (e.g. `ACM at UC Berkeley`), a specific subreddit or community (e.g. `r/developersIndia`), an active Discord server with public directory link, a specific annual conference or hackathon (e.g. `HackMIT 2026`), a specific technical newsletter (e.g. `TLDR Web Dev`).
   - Broad platforms like "Reddit", "LinkedIn", "Instagram", "Facebook", "Devpost", "Unstop", "Discord", "Twitter/X", "Meetup.com", or "YouTube" are disqualified unless the entry identifies a specific relevant community, organization, event, or node inside that platform.
2. **Verified canonical URL**:
   - Before including any entity, verify its direct canonical URL (official site, dedicated event portal, specific subreddit URL, official club page).
   - A guessed or unverified URL is never acceptable. If the page cannot be verified, discard the entity.
3. **Audience relevance with source evidence**:
   - Direct evidence showing `target_audience` is actually present (e.g. student eligibility rules, developer topic focus, attendee profiles cited on site).
4. **Activity vs. opportunity freshness**:
   - Distinguish community activity from opportunity freshness; do not apply a rigid "page updated within 3 months" rule across all types:
     - Communities / groups: Must show evidence that they are active/current (e.g. recent discussions, recent announcements, active member count).
     - Events / competitions / hackathons: Can qualify because they are upcoming, currently open for registration/submission, recurring on a documented schedule, or otherwise actionable, even if their announcement or hosting page was created more than three months ago. Stale or defunct events are disqualified.
5. **Verified engagement mechanism OR actionable opportunity (NEVER infer)**:
   - A prospect may qualify in either of two explicit ways:
     - **Path A: Documented engagement mechanism**: The source explicitly documents an available channel to engage (e.g. published sponsorship intake/prospectus, open CFP/call for speakers, mentor applications, community project showcase thread, guest workshop proposal).
     - **OR Path B: Current/upcoming/recurring actionable opportunity**: An explicitly scheduled or open event, competition, hackathon, or program relevant to the target audience that can be directly participated in or supported.
   - **Strict prohibition against inference**: NEVER infer or assume an engagement mechanism. Do NOT claim "sponsorship available", "AMA possible", "workshop partnership", "mentor intake", or "collaborative webinar" merely because such an action would be plausible. Report an engagement mechanism ONLY when a source explicitly shows that mechanism exists. If qualifying via Path B, state the exact verified opportunity rather than speculating about unverified partnerships. The evidence must make clear which case applies.

## 3. Claim-to-source traceability & anti-hallucination rules

- **Claim-to-source traceability**: Every final row must have evidence supporting ALL important claims:
  a. Entity identity and existence
  b. Audience relevance and fit
  c. Activity or scale evidence
  d. Verified engagement mechanism OR current/upcoming/recurring actionable opportunity
- **Mandatory source URLs in Evidence column**:
  - The `Evidence` column must contain the ACTUAL SOURCE URL(S) used to support those claims.
  - Do NOT merely write prose such as "official page confirms this" or "verified on their site". The source URL itself (e.g. `https://...`) MUST be present in the cell alongside concise excerpts/facts directly observed.
  - If multiple claims rely on distinct pages (e.g. entity homepage vs. sponsorship page), list both URLs in the Evidence cell separated by a space or semicolon.
- **Strict anti-hallucination**:
  - Do NOT invent or estimate: member counts, audience sizes, reach, event participation, contact information, sponsorship/partnership availability, dates, or opportunities.
  - Unknown metrics: If scale or activity numbers are not explicitly published or observable, write **Unknown**. Never guess or interpolate.
  - Contacts: If an official contact URL or public inbox is published on the site, link to it; otherwise state "None listed". Never invent names or emails.
- **Quantity cap & no padding**:
  - Return UP TO 15 qualified opportunities.
  - NEVER pad the output to reach 15. If only 5 or 7 satisfy the strict qualification standard, return 5 or 7. If none meet the bar, report an empty table with an explanation in Disqualified Entities.

## 4. Procedure

1. **Deconstruct the audience profile**:
   - Extract core attributes of `target_audience`, geographic constraints (`geography`), and exclusions (`constraints`).
   - Note key keywords, technical domains, affiliations (e.g. universities, professional titles, open-source ecosystems).
2. **Targeted web research**:
   - Search across community ecosystems:
     - Academic and student organizations (ACM, IEEE, university clubs, student developer organizations).
     - Hackathons, student competitions, and developer conferences.
     - Niche online forums, Discord directories, subreddits, Discourse forums.
     - Curated newsletters, publications, and open-source project user groups.
3. **Screen and qualify candidates**:
   - Test each candidate against the qualification criteria. Discard candidates that fail any check.
   - Verify that the entity respects `geography` and does not violate `constraints`.
4. **Extract and verify evidence**:
   - Capture verified canonical URL.
   - Note exact audience fit evidence.
   - Check dates of recent posts or upcoming event schedules.
   - Record the verified engagement route and direct source URL(s).
5. **Write the report**:
   - Write strictly to `reports/AUDIENCE_PROSPECTS.md`.

## 5. Output format

The final artifact must be written to `reports/AUDIENCE_PROSPECTS.md` using the following exact structure:

```markdown
# Audience Prospects: [Target Audience]

**Geographic Scope:** [Specified geography or Global]
**Constraints Applied:** [Specified constraints or None]
**Qualified Opportunities Found:** [Count, maximum 15]

## Qualified Opportunities

| Entity | Type | Verified URL | Audience Fit | Activity/Scale Evidence | Verified Engagement Mechanism | Evidence |
|---|---|---|---|---|---|---|
| [Specific Named Entity] | [Community / Event / Association / Newsletter / Competition] | [Verified Canonical URL] | [Evidence of target audience presence] | [Observable recent activity date or verified count, or Unknown] | [Explicit mechanism: CFP, Showcase, Sponsor Prospectus, or Upcoming Event Registration] | [Source URL(s) + concise quote/fact: e.g. https://example.org/sponsor - "Accepts track sponsors"; https://example.org/about - "2,000 CS students"] |

## Disqualified Entities & Observations

Brief summary of entities considered but disqualified (e.g. broad platform with no specific node, stale event, no explicit engagement mechanism or actionable opportunity, unverified URL), and any unmet constraints.
```

Note: Do not use unescaped vertical pipes (`|`) inside table cells; separate multiple items with semicolons or spaces so table columns parse correctly.
