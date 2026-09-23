---
name: community-signal-finder
description: Find recent high-intent public conversations that may be helped by a product, with evidence and non-promotional response guidance.
---

## Scope

Investigate only the public communities and sources supplied in the inputs. Use publicly
accessible web pages and ordinary search or browser research available inside the isolated
sandbox. Keep the research bounded by the lookback window and maximum opportunity count. Do not
use paid APIs, private groups, login-only material, scraped personal profiles, or contact data.

## Research method

1. Restate the product, target customer, and problem keywords as a concise research lens.
2. Search each supplied source for recent discussions that describe a concrete problem, request
   advice, compare solutions, ask for recommendations, or report an active workaround. Prefer
   first-party discussion pages with a stable URL and visible date.
3. Exclude generic brand mentions, jobs, news without a customer problem, obvious spam,
   promotional or affiliate posts, duplicate cross-posts, vague complaints, and conversations
   whose intent cannot be supported by the text. Never infer a person’s identity or buying
   ability beyond what they explicitly state.
4. For every retained conversation, capture the source/community, title or short description,
   URL, visible date, the problem in the speaker's terms, concise evidence, and the relevant
   target-customer signal. Quote only short snippets when necessary; do not reproduce personal
   data or whole posts.
5. Score opportunities with an explicit qualitative level: high, medium, or low. Consider
   problem/product fit, strength of intent, recency, and whether a genuinely useful response
   could be made without hijacking the discussion. A high score requires evidence, not merely a
   keyword match. Explain the main reason for each ranking.
6. Suggest a response angle that starts with useful advice, clarification, or a relevant
   resource. Do not write a sales pitch. Every opportunity must include a caution that the
   company should disclose its connection and avoid promotional or spammy replies; recommend
   not replying when useful participation is not clear.
7. Keep at most the requested number of strongest opportunities, ordered by priority. If no
   conversation meets the threshold, write a useful no-op report explaining what was searched,
   what was excluded, and what evidence was missing.

## Report contract

Write only the declared Markdown artifact. Include:

- research window and sources searched;
- a short method and limitations section;
- a prioritized table or sections containing conversation/source, date, problem, evidence or
  summary, intent level, target-customer fit, and helpful response angle;
- a clear promotional/spam caution for each opportunity;
- an explicit statement that no posting, contacting, or other external action was performed.

Separate observed evidence from interpretation. Preserve URLs exactly as found. Use
"unknown" or "not visible" for missing dates or evidence; do not fabricate recency, intent,
customer fit, or product capability.
