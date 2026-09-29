---
name: search-and-answer-engine-page
description: Research an answer page from primary sources and shape it so search engines and AI answer engines can quote it.
---

# Research and structure for search and answer engines

ANSWER_SEO_V1: the reference data carries `today`, the date the page prints as its last update.
These rules add to the evidence-first rules above; where they give an order, follow this order.

## Research

- Open the official site of every product, tool or vendor the page names: its pricing, docs or
  feature page, not only a search result snippet. A claim about a named product needs that
  product's own page as its source.
- Use at least five distinct credible sources when the topic has them, primary sources first.
  Use a review or a third-party article only for what primary sources cannot show.
- Cite inline: link the sentence that carries each claim to its source, and list every cited
  source again under `## Sources`.
- When research finds fewer than five usable sources, write a shorter page from what it found.
  Never pad the Sources list with pages the page does not use.

## Page shape

Write the page in exactly this order:

1. Frontmatter for search listings, as the first lines of the page:

   ```
   ---
   meta_title: <under 60 characters, the question or its answer>
   meta_description: <70 to 160 characters that answer the question>
   ---
   ```

   Plain text only on each line: no quotes, no line breaks, no Markdown.
2. One `# ` title: the buyer's question, or its direct answer, in plain words.
3. `Last updated: <today>` on its own line.
4. The answer: one paragraph of 40 to 60 words that answers the question completely, names the
   options when there are several, and still makes sense when quoted on its own.
5. Body sections under `## ` headings phrased as the questions a buyer asks next, each ending
   with a question mark. Open each section with one or two sentences that answer its heading.
6. A Markdown comparison table whenever the page compares two or more products, plans or
   approaches: one row per option, columns for the facts a buyer compares (price, what it
   does, limits, who it suits), every cell backed by a cited source.
7. `## FAQ` with three to five `### ` questions, each ending with a question mark and answered
   in two to four sentences.
8. `## Sources` last.

## Readability

- Keep sentences under 25 words on average and paragraphs under 100 words.
- Use lists for steps, options and criteria, and the table for side-by-side facts.
- Define a term the first time the page uses it. Prefer the words buyers use in their question.
- Write for a reader who skims: every heading and the first sentence of every section carry
  meaning without the rest of the page.
