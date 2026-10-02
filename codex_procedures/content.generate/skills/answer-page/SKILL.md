---
name: answer-page
description: Draft one answer page for a buyer question AI answers missed the site on, with a direct answer, cited sources and a search listing.
---

1. Read `content_draft.answer`: the buyer question, the audit's gap questions it came from and
   the date the page prints as its last update. Read the cited audit finding in
   `content_draft.evidence`; it says which sampled AI answers left the site out.
2. Read the positioning files in `content_draft.positioning` and the writing guide. They decide
   how the product is described; the question decides what the page answers.
3. Search the site's own pages for the question first. When one already answers it directly
   and plainly, record `already_covered` instead of writing a competing page.
4. Research the question on the public web. Open the official page of every product, tool or
   vendor the page names. Use at least three distinct credible sources, primary sources first,
   and cite each claim inline. Never invent capabilities, prices, customers or results.
5. Write the page in the order the prompt gives: the two-line search listing, the question as
   the title, `Last updated:`, a 40 to 60 word direct answer that still makes sense when quoted
   alone, question headings, an FAQ, and `## Sources` last.
6. Write for the buyer, not for Tin: never mention the audit, the plan, AI answers, the
   workflow or the drafting process in the page. Keep notes in the companion file.
