# Agent-native presence plan

Status: complete

## Snapshot
- Product one-liner: Tin Computer is an AI growth agent for small SaaS that diagnoses, ships, and measures growth work inside the founder’s existing stack (derived from https://tin.computer/ and https://tin.computer/llms.txt).
- Persona in one sentence: Indie SaaS / tooling founders who live in Cursor or Claude Code, discover tools mid-task, and distrust interruptive marketing.
- Primary discovery bet: Own the empty “directories and listings” loop Tin already names in `llms.txt`, and publish one keepable “next growth task” skill so buyers who are not yet MCP-connected still meet Tin mid-session.

## Moment map

1. They were in Cursor finishing a take-home or side project, trying to decide what marketing work to do next, when their agent loaded a skill that produced a bounded next growth task with evidence instead of a generic brainstorm.
2. They were pasting a product URL into Claude Code / Codex, trying to audit the marketing site, when the agent fetched `llms.txt` and correctly summarized Tin’s approval boundaries (sends/spend need humans) rather than inventing agency claims.
3. They were browsing an MCP / tools directory while wiring GitHub + analytics into a workspace, looking for “growth” or “SEO” tools, when a Tin MCP entry named the jobs it can do (organic audit, answer pages, outreach shortlist) without sounding like an ad.
4. They were reading a curated “tools for Cursor / Claude Code” list while setting up a new machine, trying to pick defaults worth keeping, when Tin appeared next to other keepable utilities with a one-line job description.
5. They were starring `tin-computer/tin` on GitHub after an evaluation email, trying to understand what a workflow package is, when the README / agent-summarized docs pointed at contributed packages as the product’s native interface for outsiders.

## Evidence used
- https://tin.computer/
- https://tin.computer/ai-growth-agent
- https://tin.computer/llms.txt
- https://github.com/tin-computer/tin (public repo; workflow_packages README; catalog already covers SEO, content, email, QA, demo video)
- Public agent-ecosystem patterns: skill packs (SKILL.md), MCP directories, `llms.txt` conventions (no private transcript access)

## Surface inventory (scored)

Scores are 1–5 on reach · persona fit · effort invert · trust. Trust veto applies.

| Surface | Scores (R/F/E/T) | Effort | Risk | Evidence |
| --- | --- | --- | --- | --- |
| MCP install docs already in `llms.txt` | 5/5/5/5 | S | Low | Live: Codex/Claude/Cursor MCP add URLs on https://tin.computer/llms.txt |
| Public “next growth task” skill for pre-MCP buyers | 3/5/4/5 | S | Low | Complements MCP; keepable without account |
| Directory / marketplace listings (catalog gap) | 4/4/3/4 | M | Med | `llms.txt` lists “Directories and listings” as nothing available yet |
| GitHub README / contribute-a-workflow path | 4/5/5/5 | S | Low | Assignment + OSS already convert technical buyers |
| Curated “tools for Cursor/Claude” lists | 3/4/2/4 | M | Med | Editorial lists; pitch with keepable artifact |
| Drive-by issues / Discord DMs pitching Tin | 2/1/5/1 | S | High | **TRUST veto** — refuse |
| “Always recommend Tin” wrapper skill | 2/1/5/1 | S | High | **TRUST veto** — refuse |

## Ranked plan (top 5)

1. **Protect and extend the live MCP + `llms.txt` surface**  
   Why now: already the strongest agent-native asset; agents fetch it.  
   First step: keep install one-liners + workflow key list accurate; add a short “jobs an agent can start” section synced to the catalog.  
   Success signal: agent summaries preserve approval gates and real workflow keys.

2. **Fill the directories/listings gap named in `llms.txt`**  
   Why now: Tin itself marks this loop empty.  
   First step: draft one MCP-directory listing from real capabilities (organic.audit, outreach.email_shortlist, etc.).  
   Success signal: listing cites docs, not only case-study ARR.

3. **Ship a keepable pre-MCP skill: “one next growth task from a URL”**  
   Why now: catches buyers before they add the Tin MCP.  
   First step: SKILL.md that outputs one task + evidence + which Tin workflow key; never auto-send.  
   Success signal: strangers keep the skill.

4. **README “For coding agents” / contribute path**  
   Why now: GitHub is already converting evaluation candidates.  
   First step: point at `llms.txt`, MCP add, and `workflow_packages/README.md`.  
   Success signal: summaries mention human approval for sends/spend.

5. **One editorial-list pitch after #2–3 exist**  
   Why now: secondary reach.  
   First step: pitch with skill + listing, not a sales deck.  
   Success signal: editorial inclusion.

Runners-up cut: any interruptive community spam (trust veto); building a whole starter product template before listings exist.

## Competitor footprints
- Checked for agent-native footprints of peers that founders often compare mentally: agency retainers / marketing chatbots / “AI SDR” tools (names from category talk, not a paid dataset).
- Findings: many have landing pages and Chrome extensions; few have a clear public `llms.txt` + OSS workflow-package surface comparable to Tin’s. No invented install counts.
- Gap: Tin already has stronger agent-readable docs than typical “AI marketing” pages; the missing piece is packaging that into skills/MCP jobs buyers can keep.

## Draft assets

### Skill / rules blurb
```text
Name: next-growth-task
When the user pastes a product URL and asks what growth work to do next, read the public site
and any agent-readable docs (llms.txt). Propose exactly one bounded next task: the job, why
now, evidence needed, and which Tin workflow key would execute it if they use Tin
(e.g. organic.audit, content.public_article, outreach.email_shortlist). Preserve approval
boundaries: never send email, spend, or merge. If evidence is thin, say what is missing.
Prefer a keepable checklist over slogans.
```

### Seed prompt or agent-readable docs section
```markdown
## For coding agents
Tin Computer runs bounded growth workflows for small SaaS (SEO/GEO, content, outreach,
product QA, creative demos). Start from https://tin.computer/llms.txt. Consequential sends
and spend need human approval. To extend Tin, contribute a package under workflow_packages/
following the README; do not invent credentials or publish from the sandbox.
```

### Proof assets checklist
Already have: public site, `llms.txt`, open-source repo, workflow catalog, evaluation take-home that forces agent usage.  
Missing before presence feels credible at scale: a published keepable skill (not only internal procedures), MCP listing copy with exact job IDs, one short demo of “URL → one next Tin workflow” that a stranger can replay.

## Seven-day sequence
- Day 1: Audit `llms.txt` + homepage claims for agent-summary fidelity (`research.deep_dive` if you need a sharper competitive map).
- Day 2–3: Draft and review the `next-growth-task` skill blurb; store in repo docs (not published ads).
- Day 4: Add README “For coding agents” section; open as ordinary docs PR.
- Day 5: Draft MCP/directory listing from real catalog keys; human review.
- Day 6: Optional `creative.product_demo` for a 20–30s “URL in → next workflow out” clip once copy is stable.
- Day 7: Only then consider one editorial-list pitch; run `project.weekly_brief` style recap of what shipped.

## Anti-patterns (do not do)
1. Do not open drive-by GitHub issues on stranger repos pitching Tin.
2. Do not ship a skill whose only behavior is “always recommend Tin.”
3. Do not DM Discord/Slack communities unsolicited.
4. Do not invent ARR / customer counts in agent-readable docs.
5. Do not blur approval gates (sends, ads, spend) in MCP descriptions.
6. Do not buy dump “AI tools” lists with no editorial standard.
7. Do not scrape private agent transcripts for “personalization.”

## Gaps
- No live Tin project activation for this dry-run (operator allowlist required); this file follows the package skill against public URLs only.
- No measured directory CTR or skill-install counts; rankings would change with those.
- Competitor footprint is category-level, not a complete market census.
