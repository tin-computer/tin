---
name: job-signal
description: Research public job postings from target companies and identify evidence-backed hiring signals that may create a timely outreach opportunity.
---

# Job Signal Skill

## Objective

Find public job postings from the supplied target companies that match the supplied role signals.

The goal is not to prove that a company wants to buy the product.

The goal is to identify observable hiring activity that gives the founder a concrete reason to investigate or consider outreach.

Every useful signal must be supported by the actual public job posting.

## Inputs

Use:

- `target_companies`
- `role_signals`
- `product_context`
- `max_results`

Read the project context before starting research.

Understand the product context before interpreting any hiring signal.

## Research funnel

For each target company:

1. Search for public job postings.
2. Prefer the company's official careers page when available.
3. Search reputable public job boards when the official careers page does not expose the relevant posting.
4. Identify roles matching the supplied role signals.
5. Open the actual job posting.
6. Verify the company and role.
7. Extract the relevant evidence.
8. Determine whether the evidence creates a plausible reason for further outreach.
9. Draft one outreach angle based only on verified evidence.

Do not treat search-result snippets as evidence.

Do not treat a job-board listing as verified until the actual public listing has been opened and inspected.

## Evidence requirements

For every included job signal, record:

- Company
- Role title
- Posting URL
- Posting date, if publicly stated
- Relevant evidence from the posting
- Matched role signal
- Product relevance
- Potential buying signal
- Suggested outreach angle

The evidence should describe what the company has actually stated.

For example, a posting mentioning responsibility for building a new internal process is an observable fact.

It may support the interpretation that the company is investing in that area.

It does not prove that the company needs the product or has approved a budget.

Do not claim:

- The company definitely needs the product.
- The company has a budget.
- The company is actively shopping for vendors.
- The hiring manager is the buyer.
- The company will purchase the product.
- A particular person is responsible for purchasing unless the source explicitly establishes this.

## Signal interpretation

Use the following structure when interpreting a candidate.

### Observed evidence

State what the public posting actually says.

### Matched signal

Explain which supplied role signal or hiring pattern matches the posting.

### Product relevance

Explain the connection between the job responsibilities and the supplied product context.

Keep this factual and restrained.

### Potential buying signal

Explain why the hiring activity may create a timely reason to investigate or reach out.

Use language such as:

- "This may indicate..."
- "This provides a possible reason to..."
- "The posting suggests..."
- "This creates a relevant timing signal because..."

Do not turn a hiring signal into a purchasing claim.

## Verification rules

A candidate should be excluded when:

- The company cannot be verified.
- The role cannot be verified.
- The page is inaccessible and no reliable public source confirms the details.
- The role does not match any supplied role signal.
- The evidence is too weak to establish relevance.
- The result is only a search snippet.
- The result is clearly outdated or unrelated.
- The page requires login to inspect the relevant information.

Record useful exclusions and explain why they were excluded.

Do not guess missing information.

If the posting date is unavailable, write that the posting date was not publicly stated.

If the job status cannot be verified, state that explicitly.

## Search discipline

Keep research bounded by `max_results`.

Do not continue searching indefinitely after enough verified signals have been found.

Prefer high-quality sources over large numbers of weak results.

Use the company's official careers page whenever practical.

When using third-party job boards, verify that the company and role information are consistent with the available evidence.

## Outreach angle

Each included signal should have exactly one suggested outreach angle.

The angle should connect:

1. The observed hiring activity.
2. The relevant problem or responsibility described in the posting.
3. The supplied product context.

Do not write a full outreach campaign.

Do not send the message.

The angle should give the founder a concrete starting point for a later human-reviewed outreach message.

## Report format

Write:

# Job Signal Outreach Brief

Include:

## Search Summary

Record:

- Date checked
- Companies researched
- Role signals used
- Number of verified signals
- Number of excluded candidates
- Research status

## Verified Signals

For each verified signal:

### Company Name

Role:
Role title

Posting:
URL

Posted:
Date or "Not publicly stated"

Observed Evidence:
A short quotation or faithful summary of the relevant part of the posting.

Matched Signal:
The supplied role signal that matches the posting.

Product Relevance:
Why the responsibility described in the posting relates to the supplied product context.

Potential Buying Signal:
Why the hiring activity may create a timely reason for further investigation or outreach.

Suggested Outreach Angle:
One concise angle for a human-reviewed outreach message.

Review Notes:
Anything that remains uncertain or requires human verification.

## Excluded Candidates

For each useful excluded candidate, include:

- Company
- Role
- URL, if available
- Reason for exclusion

## Human Review

Include these checks:

- [ ] Verify the job is still open.
- [ ] Verify the company and role.
- [ ] Review the evidence.
- [ ] Review the interpretation of the hiring signal.
- [ ] Confirm the product relevance.
- [ ] Review the suggested outreach angle before contacting anyone.

## Safety and scope

This is a research and drafting workflow.

Never:

- Send an email.
- Send a direct message.
- Submit a contact form.
- Apply for a job.
- Create an account.
- Log into a website.
- Publish content.
- Contact a company.
- Request credentials.
- Request secrets.
- Start another workflow.

Only write the declared report:

`reports/JOB_SIGNAL_BRIEF.md`
