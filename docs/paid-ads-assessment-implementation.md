# Paid ads assessment

`ads.assessment` tells a founder whether Google Search ads fit their business before
any ad account is connected. It is a native LLM flow: code owns the sequence, the economics, the
scoring, the verdict and the saved files; five bounded model steps read evidence, label keywords,
diagnose past campaigns and shape a first campaign around the decision. It is advisory. Nothing
in it creates, funds or changes an ad campaign. Meta is not assessed in this version; the verdict
may still be "neither".

## Inputs and the gate

The closed input schema lives in `src/tin_lite/paid_ads.py` (`INPUT_SCHEMA`). Besides the
project and product URL it asks the economics questions the Start here plan does not: price,
billing, gross margin band, the conversion event a stranger can complete today, the ad budget,
and ads history with optional spend, clicks, purchases and softer conversions. Zero means
unknown for the numbers. Three earlier runs may be named and are verified against their publish
receipts (`paid_ads_sources.py`): the Start here plan, a keyword plan and an organic audit.

`gate()` ends the run before any provider spend when the founder said no paid ads, when there is
no public site, or when nothing on the site can be completed and measured. A gated run still
publishes a short not-now report.

## Evidence and research

`paid_ads_gather` reads the site (up to eight pages, keeping tracking-tag signals from the HTML),
the verified upstream bundles, ninety days of Search Console queries when a connected property
matches the site, and the Ads Transparency Center for the own domain. `paid_ads_research` asks
the judgment model for a business profile, reuses keyword-plan seeds or asks the drafting model
for buyer-intent seeds, then buys bounded research: Keyword Planner ideas and twelve-month
volumes through the operator-run gak service (`src/tin_lite/gak.py`, zero cost), DataForSEO
keyword metrics with intent, then the drafting model labels every keyword's intent so that the
ad-traffic forecast at three bid levels and the paid-slot counts from five live result pages cover
buyer-intent terms only, competitors' paid keyword footprints and Transparency Center advertisers
matching the business name. Every call is a receipted, reserved effect under the
`paid_ads:{run}:` prefix, the ledger is bounded by `min(TIN_LITE_PAID_ADS_MAX_COST_USD,
max_cost_usd)`, and an unconfirmed call is never bought again.

Three provider facts shaped the research step: the planner expands short two-to-four-word seeds
into the long tail but returns nothing for long descriptive phrases, so the seed rules insist on
short ones; Keyword Planner URL seeding returns the domain
name's meaning rather than the product's, so URL ideas are kept only when they share a word
with a model seed; and the Transparency Center indexes ads under the advertiser's legal name,
so the lookup runs by business name as well as by domain.

## Scoring and the verdict

`paid_ads_assets/score.py` is deterministic and versioned with `benchmarks.json` and
`rubric.json`; the contract digest pins all three into the definition. The allowable cost per
customer is the lower of twelve months of gross profit and a third of lifetime value, or
first-purchase gross profit for one-time sales. The estimated conversion rate is the industry
search benchmark scaled by the conversion event, the intent mix of the labelled keywords and
landing-page readiness. The forecast bid is the highest affordable level; demand is floored at
a plain click share of buyer-intent volume because the forecast returns almost nothing for new
terms. The scorecard weighs economics 30, demand 25, readiness 20, fit 15 and auction density 10.

The verdict order is history, then economics, then budget, then score. Observed history from
the form replaces the estimate; a softer tracked event yields the downstream rate needed for
break-even instead. Captive-intent terms (a platform the product plugs into) justify a small
exact-match test even when benchmark economics say no. The scorer was calibrated on
2026-09-22 against four real projects with Google Ads history; `tests/fixtures/paid_ads/`
pins those cases.

## Model steps

`profile` and `verdict` and `diagnose` use `gpt-6-sol`; `seeds`, `classify` and `repair` use
`gpt-6-luna`. Each step has one retry under its own `:retry` id and the verdict gets one
repair pass; code then clamps anything still outside its bounds. The verdict step receives the
decision, the binding constraint and the numeric ranges as fixed inputs and may only explain,
cite evidence ids and shape the campaign inside them.

## Outputs

`reports/paid-ads/{run_id}/` holds `ASSESSMENT.md` (with one fenced `tin-ads` JSON block),
`assessment.json` (what a later setup workflow consumes), `keywords.csv` and `evidence.json`.
They publish together through the shared create-only bundle writer and the run projects
`paid_ads_assessment_ready`. Stop is available through the dashboard, the API
(`stop-paid-ads-assessment`) and MCP; a stop fences new paid work in Postgres before the
Temporal signal.

## Configuration

`TIN_LITE_PAID_ADS_MAX_COST_USD` (default 0, disabled), `TIN_LITE_GAK_URL`, `TIN_LITE_GAK_TOKEN`
(and `TIN_LITE_GAK_ALLOW_PRIVATE` for a loopback or private service address), DataForSEO
credentials and the native model key. `executor_gates.paid_ads_gate` refuses admission and
keeps the Start here plan from recommending the workflow until all are set. The workflow lives
in the Paid ads system, is listed in the dashboard and MCP, and is deliberately housekeeping
for the Start here plan so the plan never schedules it.

## Limitations

A model call refused by the spending limit is reported as a spending-limit stop.
Unconfirmed calls keep their separate error and are not purchased again.

Google Search only. Country-level targeting in English markets. The tracking scan is
heuristic. Ads history comes from the founder's numbers, not from an ad account. Provider
forecasts and benchmarks are estimates, never traffic or revenue.
