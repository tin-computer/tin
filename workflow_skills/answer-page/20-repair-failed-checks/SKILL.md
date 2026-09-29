---
name: answer-page-repair
description: Fix a researched answer page that missed Tin's page checks, without new research.
---

# Repair a page that missed its checks

ANSWER_REPAIR_V1: when the reference data carries `repair`, the research is done and the page
is drafted. `repair.page` is that draft, `repair.failed_checks` lists the exact checks it
missed, and `repair.research` holds the searches and sources behind it. This request has no
web search.

- Return the whole corrected page in the order and format the rules above give, starting
  with its frontmatter. Do not repeat the argument plan comment; Tin already saved it.
- Fix every listed check, and change only what a fix needs. Keep the question, the claims,
  the numbers and every link destination as they are.
- Use only sources that appear in `repair.page` or `repair.research`. Never add a claim, a
  number or a source the research did not find. When a check asks for more sources than the
  research found, narrow the page to what those sources support instead of inventing one.
- Split an overlong paragraph rather than cutting its facts. Turn a statement heading into
  the question the section answers.
- Return only Markdown.
