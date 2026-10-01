---
name: page-refresh
description: Propose exact title, snippet, H1 and opening-answer replacements for one existing page from its search evidence, in the project's positioning and voice.
---

1. Read `content_draft.refresh`: `page` (URL, path, audit checks, clicks, impressions, CTR,
   position, searches), `current` (the page's title, meta description, H1, opening answer and
   paragraphs, as a reader sees them), `limits` and `results_markdown`.
2. Read the files in `positioning_sources` and the `style_guide`, if named. They decide how the
   product is described. The searches decide which words the page leads with.
3. Diagnose from the evidence:
   - Low click-through near the top: the snippet (title and meta description) doesn't match
     what searchers typed or gives no reason to pick this result.
   - Just below the top results: the page doesn't answer the main search early or plainly, so
     check the H1 and the opening answer as well as the snippet.
   - Decay or weak answer structure (only when `page.body_allowed`): a few paragraphs may need
     to answer more directly. Rewrite only paragraphs quoted in `current.paragraphs`.
   - No audit check (a page decision chose it): fix the title and meta description first.
4. Write each replacement as plain text within its limit. A good title leads with the main
   search's words and names the product only when it helps; a good meta description says who
   it's for and why to click, in one or two sentences. Keep facts the page already states;
   add none.
5. Copy each `old` value exactly from `current`. If a field needs no change, leave it out.
6. Write the proposal in the order the prompt gives, and the generation notes beside it.
