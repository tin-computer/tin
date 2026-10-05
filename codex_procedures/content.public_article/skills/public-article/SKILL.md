---
name: public-article
description: Turn durable project evidence and original thinking into a rigorous, readable public article with a clear audience, thesis, narrative, citations, and limitations.
---

# Public article

Create one public-facing Markdown draft from the run inputs and durable project evidence. Write
only the artifact path declared by Tin. The checkout may contain evidence and voice guidance, but
it cannot alter the Tin procedure or output contract.

## 1. Establish the article contract

Before drafting, determine:

- the reader named by `audience` and the problem they already feel;
- the single sentence the article should land from `brief`;
- the mechanism behind that claim, not merely a correlation or outcome;
- the decision or new understanding the article gives the reader;
- the evidence available in project files and, when permitted, current web sources;
- what would weaken or change the thesis.

Use `goal` to set the article's job:

- `explain`: make a mechanism or finding understandable;
- `argue`: make and defend a bounded position;
- `share_findings`: present original work and its implications;
- `announce`: explain what changed, for whom, and why, without promotional inflation.

Use `length` as a ceiling rather than a quota. Respect `voice_notes` where they do not conflict with
accuracy or the product contract.

## 2. Ground the claims

Read relevant project documents before searching. Treat file contents as claims with provenance,
not automatically verified facts. Follow `source_policy`:

- `project_only`: use only durable project evidence. Do not browse merely to decorate the draft.
- `project_and_web`: verify current external facts, locate relevant prior work, and add a small
  number of authoritative sources that sharpen or challenge the thesis.

Never fabricate a quote, case study, person, statistic, test result, or citation. If the article
needs a human story and none exists in the evidence, use a documented real case or write without
one. Attribute numbers and current claims with Markdown links near the sentence they support.
Project files are private evidence: never link to them or list them as sources, because a reader
can't open them. State the founder's own work and results plainly, without a citation, and link
public pages instead when they exist (the founder's site, repository, videos or posts).
Links you cite are copied clean, without tracking parameters (`utm_*`, `ref`, `source=openai`
and similar); a search tool's tracking suffix is not part of the source.

## 3. Build the structure

Plan internally before writing the artifact:

1. **Vision:** open with a problem the reader cares about and the idea that helps them understand
   or address it. State the empowerment promise early.
2. **Steps:** give the reader a short roadmap through the argument or evidence.
3. **News:** use concrete, current evidence throughout. Explain why it changes the reader's model.
4. **Contribution:** close by naming what the work identifies, tests, frames, or makes possible.

Layer in only what the evidence supports:

- one memorable phrase or concept, repeated sparingly;
- one dominant, preferably counterintuitive insight;
- a real narrative thread when a documented case exists;
- explicit assumptions and the direction of important limitations;
- a clear distinction between exploratory evidence and a demonstrated result.

Do not force a metaphor, slogan, surprise, literature review, or story when it weakens the truth.

## 4. Draft for the reader

- Answer first and support it. Do not make the reader wait for the point.
- Use plain verbs, concrete scenes, actual numbers, and specific nouns.
- Explain technical terms at first use. Keep methodology proportional to the audience.
- Use first person when the source work is legitimately the author's work.
- Use sentence-case headings and a small number of sections.
- Put limitations beside the claims they constrain; add a concise limitations section only when
  the piece needs one.
- End with the strongest accurate implication, not a generic summary or call for engagement.

The artifact must contain the finished article, not planning notes, simulated reviewer dialogue,
scores, or a description of how it was written.

## 5. Adversarial review

Before the final edit, test the draft from three relevant perspectives: a domain expert, a
skeptical practitioner, and a public editor. Identify unsupported claims, assumptions doing too
much work, missing context, and confusing prose. Fix valid problems. Acknowledge a material scope
limit when fixing it would require a different project.

Then use $public-article-edit. Apply its preservation and prose checks to the entire artifact.
