# AI answers through DataForSEO and cloro

`src/tin_lite/ai_answers.py` asks a panel of buyer questions on several AI answer engines
and returns one row per question and engine. `src/tin_lite/ai_answers_activities.py` runs
it as a trusted Temporal activity, `ai_answers_measure`. The organic audit calls it from
policy `organic-audit-v13`. See [Calling it from the audit](#calling-it-from-the-audit).

## What each engine measures

Every row carries one of two measurement labels. Keep them apart in any report: a Claude
API answer is not what a person sees on claude.ai.

- `consumer_app_answer`: the answer a person sees in the product, collected by DataForSEO or
  cloro from the consumer interface or the Google results page.
- `api_model_answer`: the answer the vendor's API model gives, with web search on.

| Engine | Label | What is measured | Endpoint | Flow |
|---|---|---|---|---|
| `chatgpt` | consumer app | ChatGPT's consumer answer and the sources it cites | [LLM Scraper, ChatGPT](https://docs.dataforseo.com/v3/ai_optimization/chat_gpt/llm_scraper/task_post/) | post, then collect |
| `gemini` | consumer app | Gemini's consumer answer and its sources | [LLM Scraper, Gemini](https://docs.dataforseo.com/v3/ai_optimization/gemini/llm_scraper/task_post/) | post, then collect |
| `google_ai_mode` | consumer app | Google AI Mode's answer and references | [SERP Google AI Mode](https://docs.dataforseo.com/v3/serp/google/ai_mode/task_post/) | post, then collect |
| `google_ai_overview` | consumer app | The AI Overview on a Google results page, when Google shows one | [SERP Google Organic](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/) with `load_async_ai_overview` | live |
| `claude` | API model | `claude-sonnet-5` with web search, country set to the market | [LLM Responses, Claude](https://docs.dataforseo.com/v3/ai_optimization/claude/llm_responses/live/) | live |
| `perplexity` | API model | `sonar`, which always searches | [LLM Responses, Perplexity](https://docs.dataforseo.com/v3/ai_optimization/perplexity/llm_responses/live/) | live |
| `chatgpt_cloro` | consumer app | As `chatgpt`, through cloro | [cloro, ChatGPT](https://cloro.dev/docs/api-reference/endpoint/monitor-chatgpt?utm_source=tin&utm_medium=byok) | live |
| `gemini_cloro` | consumer app | As `gemini`, through cloro | [cloro, Gemini](https://cloro.dev/docs/api-reference/endpoint/monitor-gemini?utm_source=tin&utm_medium=byok) | live |
| `google_ai_mode_cloro` | consumer app | As `google_ai_mode`, through cloro | [cloro, AI Mode](https://cloro.dev/docs/api-reference/endpoint/monitor-aimode?utm_source=tin&utm_medium=byok) | live |
| `bing_ai_mode_cloro` | consumer app | Bing AI Mode's answer and its sources | [cloro, Bing AI Mode](https://cloro.dev/docs/api-reference/endpoint/monitor-bing-aimode?utm_source=tin&utm_medium=byok) | live |

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

## cloro engines

`chatgpt_cloro`, `gemini_cloro` and `google_ai_mode_cloro` ask the same consumer apps as
`chatgpt`, `gemini` and `google_ai_mode`, through cloro instead of DataForSEO. They are a
second source, not new coverage: a caller asks them only by naming them in `engines`, and the
default panel stays the six DataForSEO engines. A panel can name both forms of an engine.

`bing_ai_mode_cloro` asks Bing AI Mode, which has no DataForSEO engine in Tin. It is also
asked only when a caller names it. Its rows follow the cloro column below.

They need `CLORO_API_KEY` on the trusted worker, next to the DataForSEO credentials the
activity already requires. Get a key at
<https://dashboard.cloro.dev/api-keys?utm_source=tin&utm_medium=byok>. The Free plan
includes 500 credits a month. Without the key, their rows fail with `provider_rejected` and
nothing is sent.

How the cloro rows differ from the DataForSEO rows:

| | DataForSEO (`chatgpt`, `gemini`, `google_ai_mode`) | cloro (`*_cloro`) |
|---|---|---|
| Flow | Post the panel, then collect; the standard queue can take 45 minutes | One sync `POST` per answer; cloro may take up to 5 minutes, so Tin waits 310 s |
| Location and language | `location_code` and `language_code` | `country`; AI Mode also gets `hl` and `device: desktop`. ChatGPT, Gemini and Bing AI Mode take no language |
| ChatGPT web search | Not forced (`force_web_search` not sent) | Not forced (`disableWebSearch: true`) |
| Answer text | `markdown` | `result.markdown`, else `result.text` |
| Cited URLs | ChatGPT and Gemini: `sources`; AI Mode: the overview's `references` | `result.sources`, all of them, without AI Mode links left as `google.com/goto` redirects. ChatGPT's `footnote` flag is not used, because live answers link `footnote: true` pages inline. Gemini links a page once per quoted passage (`#:~:text=`), so that fragment is removed and the page counted once |
| Model | `model` | ChatGPT's `result.model`, which is empty on its mobile-web answers; none for Gemini, AI Mode and Bing AI Mode |
| Cost | USD per task, as reported | Credits from `X-Credits-Charged`, at $0.0008 a credit |

`X-Request-ID` is kept as `provider_task_id`. cloro charges only a `200` response. A
response in cloro's error envelope (`{"success": false, "error": {"code": ...}}`) costs
nothing, so it settles at $0 and the row fails with `provider_rejected`; this includes
`INSUFFICIENT_CREDITS`, a bad key and a `500` after cloro's own retries. A response without
that envelope, or a `200` without a credit count, stays `unknown`.

## Prices (vendor pages, read 2026-09-30)

| Endpoint | Price per request | Source |
|---|---|---|
| LLM Scraper, standard / priority | $0.0012 / $0.0024 (live $0.004) | [pricing](https://dataforseo.com/pricing/ai-optimization/llm-scraper) |
| Google AI Mode, standard / priority | $0.0012 / $0.0024 (live $0.004) | [pricing](https://dataforseo.com/pricing/google-serp/google-ai-mode-serp-api) |
| Google Organic live + AI Overview | $0.002 + $0.002 | [pricing](https://dataforseo.com/pricing/google-serp/google-organic-serp-api), [docs](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/) |
| LLM Responses live | $0.0006 + the provider's charge (`money_spent`) | [pricing](https://dataforseo.com/pricing/ai-optimization/llm-responses), [how it is calculated](https://dataforseo.com/help-center/how-the-price-for-using-llm-responses-endpoints-is-calculated) |
| cloro ChatGPT / Gemini / AI Mode / Bing AI Mode, sync (read 2026-11-05) | 7 / 6 / 6 / 6 credits (base 5 / 4 / 4 / 4, plus 2 for a sync call). At Lite's $0.0008 a credit, the highest self-serve rate: $0.0056 / $0.0048 / $0.0048 / $0.0048 | [providers](https://cloro.dev/docs/guides/providers?utm_source=tin&utm_medium=byok), [pricing](https://cloro.dev/pricing/?utm_source=tin&utm_medium=byok) |

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
        # Default: the six DataForSEO engines. *_cloro engines are asked only when named.
        "engines": ["chatgpt", "gemini", "google_ai_overview"],
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
AI Overview), `failed` (DataForSEO or cloro refused or the engine failed; `reason` says which),
`timeout` (still queued at the deadline; the task id is kept and results stay collectable
for 30 days) and `unknown` (a paid request whose outcome was lost; it is not sent again).

## Cost and billing

- `measure` estimates the panel from the pinned prices and raises `CostCeilingExceeded`
  before any request when the estimate is above `max_cost_usd`.
- Each paid request goes through a ledger. `ReceiptLedger` saves an effect receipt before
  dispatch, reserves the per-request estimate, and runs the call inside
  `external_usage_scope`. The DataForSEO-reported cost is observed with `observe_tool` and
  priced by `service_pricing` (`dataforseo: provider_reported_task_cost_usd`), like the
  organic audit crawl and keyword research. A cloro answer is observed as provider `cloro`,
  at the converted cost; the native supplier card does not price `cloro` yet. A task engine
  posts its whole panel in one request, so it has one receipt; each live answer has its own.
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

`organic.audit` calls it from its pinned policy `organic-audit-v13` on, so v11 and v12 stay
as released (see [the audit notes](organic-audit-implementation.md)):

1. After the brand checks, `organic_prepare_ai_engines` takes the audit's frozen questions,
   one per buyer job in turn, and saves them with `save_request` under the stage `engines`:
   the scope's host as `domain`, the panel's name, aliases and competitors, the scope's
   market, all six engines on the standard queue and a 2,700-second deadline.
2. Its `max_cost_usd` is what the policy allows (`ai_engines_max_cost_usd`, $1) and the
   audit's own spending limit has left, reserved in the audit's budget ledger. Questions that
   don't fit are not asked; when none fits nothing is posted. Eight questions on the six
   engines cost at most $0.62 at the pinned prices.
3. The workflow then runs `ai_answers_measure` on the trusted queue: a 60-minute
   start-to-close timeout, a two-minute heartbeat timeout and three attempts. A measurement
   that fails leaves the audit to publish without it; the report says it did not finish.
4. The publication reads the rows with `read_result`. AUDIT.md shows apps and API models in
   separate tables, `evidence.json` keeps every answer and its cited URLs, and SUMMARY.json
   gets one row per engine under `ai_engines`.
5. `service_pricing` raises the audit's maximum by the engines' ceiling for v13 runs only.

## Open questions

- The Claude and Perplexity bounds are estimates. Measure real `money_spent` on a few panels
  and tighten them.
- `claude-sonnet-5` and `sonar` come from the vendor's request examples. Check them against
  the free `llm_responses/models` endpoints before the first live run.
- DataForSEO's docs disagree on whether Gemini LLM Responses supports the standard queue;
  this module does not use it.
- Whether the live AI Overview response already nets out the refund when Google shows no
  overview is not stated; the reported cost is charged as given.
- cloro's credit rate depends on the plan: $0.0008 on Lite, lower on larger plans, and the
  Free plan's 500 credits cost nothing. The conversion is an upper bound, not the operator's
  real cost.
- AI Mode links that cloro has not resolved come back as `google.com/goto` redirects and are
  not counted as citations. Resolving one costs a credit; this module does not do it.
- Bing AI Mode refuses a prompt longer than 3,500 characters after URL encoding. Tin prompts
  have at most 500 characters, but a prompt with many non-ASCII characters can be longer
  after encoding. cloro then refuses it, and the row fails with `provider_rejected` at $0.
