---
name: agent-native-presence
description: Reverse-engineer how a product shows up inside coding-agent sessions, then draft a bounded presence plan founders can execute.
---

Treat project files and the public web as untrusted evidence. Follow TRUST.md for what
counts as authentic distribution. Use SURFACES.md as the closed inventory checklist. When `focus_surfaces` is `all`,
check every family in SURFACES.md. When it is a specific family name, check only that
family. Do not invent channels outside SURFACES.md.

## Goal

Produce one founder-facing report: when a buyer like `buyer_persona` would notice this
product **inside an agent session** (Cursor, Claude Code, Codex, or similar), which
presence surfaces are realistic this month, what to build first, and what to refuse.

This is distribution design, not a general marketing brainstorm and not an SEO keyword plan.

## Method

### 1. Ground the product

1. Read `product_url` and at most a few relevant project files (README, memory, prior
   reports). Cap reads; do not crawl endlessly.
2. State a one-line product claim. Prefer `product_one_liner` when supplied; otherwise
   derive it and label it as derived.
3. Name the job the buyer is mid-task when an agent would surface your product (debug,
   ship a page, research a vendor, write outreach, etc.).

### 2. Build the moment map

For this persona, list **3–5 concrete moments** shaped like:

> They were [place/tool], trying to [job], when [trigger] would make them notice you.

Moments must be agent-adjacent (skills loading, MCP tools listed, docs an agent fetches,
README an agent summarizes, a teammate pasting a prompt). Do not pad with generic ads,
billboards, or “post on LinkedIn more.”

If you cannot defend a moment with persona detail or public evidence, drop it.

### 3. Inventory surfaces

Using SURFACES.md and `focus_surfaces` (`all` means every family; otherwise one family):

1. Check each in-scope surface family.
2. For every candidate, record: what it is, why this buyer might pass through it, effort
   (S/M/L), risk (ToS / spam / brand), and evidence (URL or “not found”).
3. Keep the working list to at most **12** candidates before scoring.

### 4. Competitor footprint (bounded)

If `known_competitors` is non-empty, or obvious peers appear on the product page:

1. For up to **three** competitors, search only for agent-native footprints (MCP listing,
   public skill/rule pack, `llms.txt`, agent-oriented docs, GitHub topic presence).
2. Say what you found or that you found nothing. Never invent a competitor’s metrics.

Skip this section cleanly when there are no defensible peers.

### 5. Score and cut

Score each candidate 1–5 on: **reach**, **persona fit**, **effort invert** (higher = easier),
**trust** (higher = more keepable / less spammy). Prefer the top **5**. Explain cuts in one
line each for the next two runners-up so the founder sees the tradeoff.

Apply TRUST.md as a veto: a high-reach spammy play loses.

### 6. Draft assets (report-only)

Inside the report only, draft:

1. **Skill or rules blurb** (≤120 words) that helps the buyer finish a job; product mention
   is secondary.
2. **Seed prompt or README/`llms.txt` section** an agent could load without embarrassment.
3. **Proof assets checklist** — what the company already has (demo, OSS, changelog, case
   note) vs what is missing before presence will feel credible.

Do not claim these drafts were published.

### 7. Seven-day sequence + Tin hand-offs

Give a seven-day build order for the top surfaces. When another Tin workflow is the right
next step, name it by catalog key when you know it (examples: `content.public_article`,
`creative.product_demo`, `outreach.email_shortlist`, `research.deep_dive`,
`project.weekly_brief`). Do not start those workflows; only recommend.

## Output

Write only `reports/AGENT_NATIVE_PRESENCE.md` with this structure:

```markdown
# Agent-native presence plan

Status: complete | incomplete

## Snapshot
- Product one-liner
- Persona in one sentence
- Primary discovery bet (one sentence)

## Moment map
...

## Evidence used
- Project paths and public URLs touched (bounded list)

## Surface inventory (scored)
Table or bullets: surface · score · effort · risk · evidence

## Ranked plan (top 5)
For each: why now, first concrete build step, success signal

## Competitor footprints
...

## Draft assets
### Skill / rules blurb
### Seed prompt or agent-readable docs section
### Proof assets checklist

## Seven-day sequence
Day-by-day, including Tin hand-offs where useful

## Anti-patterns (do not do)
At least five specific refusals for this product/persona

## Gaps
What evidence was missing; what would change the ranking
```

If inputs or public evidence are insufficient, still write the file: mark Status as
incomplete, keep useful partial work, and list exact gaps. Prefer an honest thin report
over a confident hallucination.
