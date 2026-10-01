# AI answers through DataForSEO

`src/tin_lite/ai_answers.py` asks a panel of buyer questions on several AI answer engines
and returns one row per question and engine. `src/tin_lite/ai_answers_activities.py` runs
it as a trusted Temporal activity, `ai_answers_measure`. Nothing calls it yet: the organic
audit will, after audit v11 (#245) merges. See [Calling it from the audit](#calling-it-from-the-audit).

## What each engine measures

Every row carries one of two measurement labels. Keep them apart in any report: a Claude
API answer is not what a person sees on claude.ai.

- `consumer_app_answer`: the answer a person sees in the product, collected by DataForSEO
  from the consumer interface or the Google results page.
- `api_model_answer`: the answer the vendor's API model gives, with web search on.

| Engine | Label | What is measured | DataForSEO endpoint | Flow |
|---|---|---|---|---|
| `chatgpt` | consumer app | ChatGPT's consumer answer and the sources it cites | [LLM Scraper, ChatGPT](https://docs.dataforseo.com/v3/ai_optimization/chat_gpt/llm_scraper/task_post/) | post, then collect |
| `gemini` | consumer app | Gemini's consumer answer and its sources | [LLM Scraper, Gemini](https://docs.dataforseo.com/v3/ai_optimization/gemini/llm_scraper/task_post/) | post, then collect |
| `google_ai_mode` | consumer app | Google AI Mode's answer and references | [SERP Google AI Mode](https://docs.dataforseo.com/v3/serp/google/ai_mode/task_post/) | post, then collect |
| `google_ai_overview` | consumer app | The AI Overview on a Google results page, when Google shows one | [SERP Google Organic](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/) with `load_async_ai_overview` | live |
| `claude` | API model | `claude-sonnet-5` with web search, country set to the market | [LLM Responses, Claude](https://docs.dataforseo.com/v3/ai_optimization/claude/llm_responses/live/) | live |
| `perplexity` | API model | `sonar`, which always searches | [LLM Responses, Perplexity](https://docs.dataforseo.com/v3/ai_optimization/perplexity/llm_responses/live/) | live |

Not covered: Microsoft Copilot and Grok (DataForSEO offers neither), and the consumer apps
claude.ai and perplexity.ai. ChatGPT is asked without `force_web_search`, so the row shows
what the app decides to do; its `search_results` (retrieved but not cited) are ignored and
only `sources` count as citations.

Why some engines run live:

- LLM Responses charges $0.0006 plus the model provider's cost. On the standard queue the
  post reports only a $0.01 advance, so the real cost would appear only later. The live call
  reports the final cost, which is what billing needs. Perplexity is live-only anyway.
- The Google Organic call charges extra for the AI Overview and refunds it when the page has
  none. The live response reports the charge for the page it returned.

## Prices (vendor pages, read 2026-09-30)

| Endpoint | Price per request | Source |
|---|---|---|
| LLM Scraper, standard / priority | $0.0012 / $0.0024 (live $0.004) | [pricing](https://dataforseo.com/pricing/ai-optimization/llm-scraper) |
| Google AI Mode, standard / priority | $0.0012 / $0.0024 (live $0.004) | [pricing](https://dataforseo.com/pricing/google-serp/google-ai-mode-serp-api) |
| Google Organic live + AI Overview | $0.002 + $0.002 | [pricing](https://dataforseo.com/pricing/google-serp/google-organic-serp-api), [docs](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/) |
| LLM Responses live | $0.0006 + the provider's charge (`money_spent`) | [pricing](https://dataforseo.com/pricing/ai-optimization/llm-responses), [how it is calculated](https://dataforseo.com/help-center/how-the-price-for-using-llm-responses-endpoints-is-calculated) |

Collecting a posted task is free for 30 days. DataForSEO does not publish per-model LLM
Responses prices, so the estimate uses Tin's own bounds: $0.05 per Claude answer and $0.02
per Perplexity answer, each capped at 1,024 output tokens. With every engine on the standard
queue, a 20-question panel is estimated at $1.55 and a 40-question panel at $3.10. ChatGPT,
Gemini and AI Overview alone cost $0.26 for 40 questions.

## Calling it

```python
request = AIAnswersRequest.from_inputs(
    {
        "prompts": ["best form builder for startups", ...],  # 1-40, at most 500 characters each
        "engines": ["chatgpt", "gemini", "google_ai_overview"],  # default: all six
        "market": "US",  # US, GB, CA or AU
        "language_code": "en",
        "priority": "standard",  # or "high" for the queued engines
        "deadline_seconds": 2700,  # 60-3600; counted from the first post
        "max_cost_usd": "2",
        "brand": {
            "name": "Acme Forms",
            "domain": "acmeforms.io",
            "aliases": ["Acme"],
            "competitors": ["Formly"],
        },
    }
)
result = await measure(client, request, ledger=ledger, tag="tin-ai:<run>:<stage>")
```

`measure` returns `{"rows": [...], "summary": {...}}`. Each row has `prompt_index`, `prompt`,
`engine`, `measurement`, `surface`, `model`, `status`, `reason`, `answer` (trimmed to 4,000
characters, `answer_truncated`), `cited_urls` (at most 20), `brand`, `cost_usd` and
`provider_task_id`. The summary counts rows by status and by brand result, per engine and in
total, with the reported cost, the estimate and the ceiling.

Brand results, read from the full answer before trimming:

- `mentioned`: the name, an alias or the domain appears as a whole word.
- `cited`: a cited URL is on the domain or one of its subdomains.
- `recommended_first`: the brand is named in the answer's first list item (numbered, bulleted,
  bold or as a heading), or in its first sentence when the answer has no list, and no listed
  competitor is named earlier.

Row statuses: `answered`, `no_answer` (the engine answered with nothing, or Google showed no
AI Overview), `failed` (DataForSEO refused or the engine failed; `reason` says which),
`timeout` (still queued at the deadline; the task id is kept and results stay collectable
for 30 days) and `unknown` (a paid request whose outcome was lost; it is not sent again).

## Cost and billing

- `measure` estimates the panel from the pinned prices and raises `CostCeilingExceeded`
  before any request when the estimate is above `max_cost_usd`.
- Each paid request goes through a ledger. `ReceiptLedger` saves an effect receipt before
  dispatch, reserves the per-request estimate, and runs the call inside
  `external_usage_scope`. The DataForSEO-reported cost is observed with `observe_tool` and
  priced by `service_pricing` (`dataforseo: provider_reported_task_cost_usd`), like the
  organic audit crawl and keyword research. A task engine posts its whole panel in one
  request, so it has one receipt; each live answer has its own.
- A completed receipt is reused on retry. An attempt with no saved outcome becomes `unknown`
  and stays unconfirmed for billing's reconciliation; it is never counted as free.
- A refusal of the whole request (HTTP 401, 402, 403 or 429, or an error status with no
  tasks) is a known outcome: it settles at the reported cost, usually $0, and its rows fail
  with `provider_rejected`. A server error or an unreadable answer stays `unknown`.
- A run's own spending limit refuses the reservation, and the rows fail with
  `spending_limit`.

## The activity

`ai_answers_measure({"run_id": ..., "stage": ...})` reads the panel saved with
`save_request`, runs `measure` with a heartbeat every 20 seconds, saves rows and summary
under the `ai-answers:<run>:<stage>:result` receipt and returns the summary only. Temporal
never sees a prompt, brand or answer. A second run of the activity returns the saved summary
without calling DataForSEO. It sits on the trusted worker lane (`activity_lanes.py`).

## Calling it from the audit

After #245 merges, a later change wires it into `organic.audit` under a new pinned policy,
so v11 stays as released:

1. In the panel step, after the panel is reviewed, call `save_request` with the panel's
   questions, the scope's host as `domain`, the resolved brand name and aliases, the scope's
   market and a share of the audit ceiling as `max_cost_usd`.
2. In the workflow, run `ai_answers_measure` on the trusted queue with a start-to-close
   timeout above the deadline (about 50 minutes), a heartbeat timeout of about a minute and
   the usual bounded retry policy.
3. In the report step, read the rows with `read_result` and grade them next to the current
   OpenAI web-search answers, labelled by measurement kind.
4. Raise `AUDIT_MAXIMUM_USD` by the panel's estimate and extend `tests/test_organic_ceilings.py`.
   A `visibility.audit` caller would also need `tool` in its billed operations.

## Open questions

- The Claude and Perplexity bounds are estimates. Measure real `money_spent` on a few panels
  and tighten them.
- `claude-sonnet-5` and `sonar` come from the vendor's request examples. Check them against
  the free `llm_responses/models` endpoints before the first live run.
- DataForSEO's docs disagree on whether Gemini LLM Responses supports the standard queue;
  this module does not use it.
- Whether the live AI Overview response already nets out the refund when Google shows no
  overview is not stated; the reported cost is charged as given.
