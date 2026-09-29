---
name: search-and-answer-engines
description: Shape a public article so search engines and AI answer engines can find, quote and cite it, without changing what the evidence supports.
---

# Structure for search and answer engines

Apply these rules with the public-article skill. They shape the article; they never outrank
accuracy, the brief, the audience, `goal` or `voice_notes`. Where a rule would weaken the
article, leave it out rather than forcing it.

## Search listing

Start the artifact with frontmatter for search listings, then the `# ` title:

```
---
meta_title: "<under 60 characters: the article's question or its answer>"
meta_description: "<70 to 160 characters that state what the article answers or argues>"
---
```

Wrap each value in double quotes and escape any double quote inside it. Plain text only: no
Markdown and no line breaks. Add no other keys and put nothing else before the title.

## Article shape

1. Title: the reader's question or the article's answer, in plain words.
2. Answer first: the paragraph after the title answers the question or states the thesis in
   40 to 60 words, and still makes sense when quoted on its own.
3. Question headings: when a section answers a question the reader asks next, phrase its `## `
   heading as that question, ending with a question mark, and open the section with one or two
   sentences that answer it. These are not decorative rhetorical questions. Sections that carry
   a story or an argument may keep plain headings.
4. Comparison table: when the article compares two or more products, plans or approaches, add
   a Markdown table with one row per option and columns for the facts a reader compares, every
   cell backed by a cited source.
5. FAQ, where it fits: when readers will have follow-up questions the body does not answer, add
   `## FAQ` with three to five `### ` questions, each ending with a question mark and answered in
   two to four sentences. Leave it out of an argument or an announcement it would dilute.
6. `## Sources` last: cite inline, linking the sentence that carries each claim, and list every
   cited source again at the end. With `source_policy: project_only`, list the project documents
   the article relies on instead of web pages. Never pad the list with sources the article does
   not use.

## Readability

- Keep sentences under 25 words on average and paragraphs under 100 words.
- Use lists for steps, options and criteria, and the table for side-by-side facts.
- Define a term the first time the article uses it. Prefer the words readers use in their
  questions.
- Write for a reader who skims: every heading and the first sentence of every section carry
  meaning without the rest of the article.
