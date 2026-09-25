# High-Intent Conversations

Read the project context and the workflow inputs before doing any research.

Use the workflow inputs from `context.inputs`:

- `product_context` — product or company context
- `target_customer` — target customer
- `problem` — problem the product solves
- `keywords` — optional search keywords

Identify public conversations that indicate a potential customer is experiencing the problem described by the user, then produce a prioritized, human-reviewable response brief.

Use available public web/search capabilities in the procedure environment. Prefer first-hand discussions from real people over generic articles, company marketing pages, or SEO content.

Follow the `high-intent-conversations` skill for the detailed research and evaluation process.

The final artifact must:

1. Identify the most relevant public conversations found.
2. Include the source URL and source/community name when available.
3. Explain what evidence in each conversation indicates the underlying problem or intent.
4. Explain why the conversation is relevant to the stated target customer and product.
5. Prioritize the opportunities using the criteria defined by the skill.
6. Suggest a context-aware response angle for human review rather than a generic sales pitch.
7. Clearly distinguish observed evidence from inference.
8. State when evidence is insufficient instead of inventing details.

Do not post, comment, contact, message, or otherwise engage with anyone automatically.

Do not fabricate conversations, quotes, URLs, dates, customer details, or product facts.

Write only the declared output artifact:

`reports/HIGH_INTENT_CONVERSATIONS.md`

Do not modify or create any other project files.

