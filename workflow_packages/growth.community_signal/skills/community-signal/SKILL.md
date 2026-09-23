---
name: community-signal
description: Mine public communities for high-intent customer conversations and prepare a human-reviewable response queue.
---

# Community signal

The unit of work is a conversation, not a contact.

1. Understand the product from `product_url` and repository evidence. Extract the actual problem
   solved, the people who experience it, the situations that trigger it, and claims the product
   can safely make. Do not invent an ICP from generic marketing language.
2. Translate `pain_points` into a small set of search phrases and variants. Include problem language,
   comparison/recommendation language, and trigger language. Search the selected public sources
   through the browser.
3. Prefer conversations that are:
   - recent enough that a reply can still matter;
   - written by or clearly relevant to the target audience;
   - problem-led rather than promotional;
   - specific enough that a useful answer can be written;
   - open to outside answers according to visible community rules.
4. Score opportunities internally using four signals: audience fit, problem intensity, recency,
   and answerability. Do not expose fake numerical precision. Use `high`, `medium`, or `low` and
   explain the evidence behind the priority.
5. Exclude generic founder chatter, obvious SEO spam, duplicate threads, conversations already
   answered well, and threads whose rules visibly prohibit this kind of participation.
6. Draft a response that is useful without the product. Only suggest a product mention when the
   conversation explicitly asks for a tool, recommendation, workflow, or solution where the product
   is genuinely relevant. Never force a CTA, fake personal experience, or astroturfed endorsement.
7. Capture the exact public URL and enough context for a human to verify the opportunity. Keep
   personal data to the minimum needed.
8. Write the declared Markdown report. Each row should answer: "Why this person/thread, why now,
   what could we contribute, and what would success look like?"
9. Run `git diff --check`. Do not commit, push, post, comment, or contact anyone. Tin owns PR delivery.

A useful result is a short queue a founder can act on in 10–15 minutes, not a giant lead list.
