# Google Ads launch and monitor

`ads.launch` turns a successful paid ads assessment into one live Google Search
campaign in the founder's own Google Ads account, and `ads.monitor` keeps that
campaign healthy. Both are native LLM flows: code owns the sequence, the structure, the
budget, the bids, every rule and every write; bounded model steps write the ads, classify
search terms and explain the day. Nothing reaches Google Ads before the founder approves the
exact plan, and the monitor's budget and bidding changes wait for approval too.

## The connection

`ads.google` is an identifier-entry integration, not OAuth. The founder enters the ten-digit
customer id; Tin's manager account (`TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID`, a deployment
credential minted once against the Google OAuth client) sends a manager invitation
(`customerClientLinks`, status `PENDING`), and the founder accepts it inside Google Ads under
Admin, Access and security, Managers. Accepting inside their own account is the proof of
ownership; Tin stores no Google credential of theirs. The card shows the link state, then
billing and whether any conversion action records data (`configuration.health`). Every
request runs under the manager token with `login-customer-id` set, is bounded by the adapter in
`src/tin_lite/google_ads.py` (allowlisted endpoints, fixed GAQL in
`google_ads_requests.QUERIES`, 4 MB responses, credential scrub, opaque error codes, retries
only on transient codes) and leaves an `integration_call_receipts` row. Disconnecting ends the
manager link from Tin's side.

## Inputs and the gate

`paid_ads_launch.INPUT_SCHEMA` takes the assessment run (verified against its publish receipt
by `paid_ads_sources.pinned_bundle`), an optional daily budget and click ceiling, an optional
landing page and campaign name, notes and a model-cost ceiling. `gate()` decides one outcome
from the assessment, the account facts (status, billing, conversion actions with data in the
last thirty days) and the landing page:

- `ready`: the plan is drafted for approval.
- `needs_tracking`: no conversion action has recorded data, or the landing page carries no
  Google Ads tag. The founder approves a tracking setup instead: Tin creates or reuses one
  conversion action, reads its tag snippets, and opens a GitHub pull request inserting the site
  tag when the repository is connected with write access. The run ends with `RESULT.md`
  telling the founder where the event snippet goes; they run the launch again once conversions
  record. This is the hard block.
- `needs_billing`, `needs_link`, `account_disabled`, `campaign_exists`,
  `landing_page_unreachable`, `not_recommended`: a `SETUP.md` note is published and the run ends
  failed with that reason, since a review-pinned run cannot succeed without an approval.

## The plan and the one approval

`plan_skeleton()` decides everything code can: the campaign name carrying a deterministic
marker, the budget, the click ceiling (a tenth of the daily budget or 1.2 times the median high
bid, whichever is lower), the markets, the landing page, the ad groups with exact and phrase
keywords from `keywords.csv` (never broad; a group the assessment named as held back, such as
"pending purchase evidence", is created paused), and the shared negative list from the assessment,
the starter list in `paid_ads_launch_assets/negatives.json` and the irrelevant keywords. Model
steps then write twelve headlines and four descriptions per ad group, sitelinks and callouts
(`paid-ads-launch-copy-v1`, `gpt-6-sol`), expand the negative themes and write the founder
brief; code validates lengths, duplicates, capitals, superlatives and competitor names, allows
one repair pass, and publishes `ads/google/{run}/PLAN.md`, `plan.json` and `negatives.csv`.
The run then requests the single human review; the review's summary says which of the two
approvals it is. Nothing is written to Google Ads before `record_human_review`.

## Applying it

The whole structure is one `googleAds:mutate` request with temporary ids: budget, campaign
(paused, Search only, search partners and Display off, presence-only geo, AI Max off, text asset
automation opted out, the EU political advertising declaration, a tracking template), location
criteria, the shared negative set and its criteria, ad groups, keywords, responsive search
ads, sitelinks and callouts. `apply:validate` sends it with `validateOnly`; any error ends the
run with the opaque code and nothing created. `apply:create` sends it for real; Google applies it
atomically. If that attempt cannot be confirmed the receipt stays `unknown`, and the next
attempt looks the campaign up by its marker name and adopts it rather than sending the bundle
again. `apply:subscriptions` pauses any auto-apply recommendation subscriptions that read back
enabled, and `apply:enable` switches the campaign on; if that answer is lost,
`apply:enable_check` reads the campaign back and the launch goes on only when it reads
`ENABLED`. A timed-out, dropped or unreadable answer to a write is `unknown`, never a
refusal. Each step is its own receipt under
`paid_ads_launch:{run}:apply:*`; a stop before `enable` leaves the campaign paused. `RESULT.md`
and `campaign.json` (what the monitor consumes) publish under `ads/google/{run}/`, and
`complete_paid_ads_launch` marks the campaign row `live`.

## The monitor

`ads.monitor` runs on demand, daily or weekly against a launch whose campaign row is
`live`. It reads the campaign over seven, fourteen and thirty days, the search terms, keywords
with quality scores, ad policy status, assets, conversion actions and recommendation
subscriptions, each a zero-cost receipt. `paid_ads_monitor.decide()` is deterministic:

- Days one to three after enabling are quiet: findings only.
- Automatic: search terms the drafting model labels irrelevant or job-or-free, with at least
  two clicks and no conversion, become phrase negatives on the shared list (capped per run);
  disapproved ads are paused; keywords that spent three times the allowable cost per customer
  with no conversion after thirty days are paused.
- Proposals, at most one per run: switch to Maximise Conversions at fifteen conversions in
  thirty days, to Target CPA at thirty; raise the budget a fifth when Google reports it budget
  limited and the fortnight's cost per conversion is inside the allowable, lower it when the
  fortnight spent three times the allowable with nothing to show.
- Alerts when AI Max turns itself on, a migration date appears, ads are disapproved, the
  campaign is misconfigured or paused, or conversions stop recording.

Proposals are rows in `paid_ads_proposals` (one open per campaign) with a published document
under `ads/google/{launch}/proposals/`, surfaced as a needs-you activity. Approving through
`POST /api/paid-ads/proposals/{id}/approve` or the `approve_paid_ads_proposal` MCP tool applies
the one change under an effect lock; an unconfirmed attempt settles `unknown` and the next
monitor run reconciles from the account. Discarding changes nothing. The day's report publishes
under `ads/google/{launch}/monitor/{run}.md` and the run finishes; it never waits for anyone.

## Configuration

`TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID`, `TIN_LITE_GOOGLE_ADS_MANAGER_REFRESH_TOKEN`
(minted with the existing `TIN_LITE_GOOGLE_OAUTH_CLIENT_ID`/`SECRET`, or with the dedicated
`TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_ID`/`SECRET` pair when the token came from another client), optional
`TIN_LITE_GOOGLE_ADS_DEVELOPER_TOKEN` (sent but ignored by Google since September 2026) and
`TIN_LITE_GOOGLE_ADS_API_VERSION` (`v25`). The Google Ads API must be enabled on the same Cloud
project as the OAuth client, and that project needs at least Basic access. `executor_gates.
google_ads_gate` refuses admission until these are set. The launch is housekeeping for the Start
here plan (never auto-scheduled); the monitor may be scheduled once a campaign is live.

## Limitations

Google Search only, one campaign per launch, English-language copy. Structured snippets, offline
conversion import and the Decisions-page listing of proposals are follow-ups. Google's review of
new ads is reported by the monitor, not awaited by the launch. Account-level automated assets
can only be turned off in the Google Ads interface.
