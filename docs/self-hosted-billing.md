# Optional billing for self-hosted Tin

Tin uses one engine and migration history. Customer payments are a deployment capability,
not a requirement for workflows, project files, MCP, or usage observations.

## Self-hosted default

`TIN_LITE_BILLING_ENABLED=false` is the default. No Stripe secrets are required. The runtime
does not attach prepaid admission/settlement, start payment reconciliation, register payment
HTTP routes/webhooks or expose billing MCP tools. The dashboard does not request billing state,
offer payment navigation or process stale Checkout-return parameters. Ordinary run usage remains
available. Billing tables remain empty in the shared migration chain.

Operators supply their own infrastructure and supported provider access; disabling customer
payments does not remove provider costs or other deployment prerequisites. Native model-service
usage and Codex API usage have separate trusted accounting contracts. API-enabled Codex runs
use the server-held `TIN_LITE_LUNA_API_KEY` through a protected relay; historical OAuth runs
retain their pinned authentication. Self-host operators can select API execution using
`TIN_LITE_CODEX_API_PROJECTS` and the required runtime images without enabling customer billing.
An entirely fresh no-OAuth deployment has not yet passed clean-clone acceptance; see
[self-hosting limits](feature-status.md#source-release-and-self-hosting-limits). Existing workflow resource bounds,
provider spending ceilings, fencing, permissions, review and overlap rules still apply. This
change does not add a new general self-hosted concurrency/operational-budget administration UI.

## Hosted test deployment

Set `TIN_LITE_BILLING_ENABLED=true` to attach the billing service and expose its APIs/tools.
`TIN_LITE_BILLING_TEST_ENABLED=true` separately enables the current test-only payment/admission
pilot. Supply the deployment's Stripe test secret and webhook secret. Credentials alone do
not enroll workspaces or enable billing.

Hosted Tin also enables `TIN_LITE_BILLING_HOSTED_DEFAULTS_ENABLED=true` together with welcome
credits. On authenticated project discovery this enables run billing for workspaces with
established ownership and creates missing $10 per-run/$10 monthly policies. Existing ownership, balances and limits are preserved. New paid schedules still
need explicit standing spending authority. Supported new Codex executions use API runners
without a per-project API allowlist; private workflow execution keeps its separate pilot gate.
Without hosted defaults, enrollment remains an explicit operator action.

These defaults are [deployed and accepted](studio-api-and-hosted-credits.md), including Studio
model and voice costs. Self-host defaults remain off. Starts use configured conservative
estimates and internal per-provider-call funding, not mandatory quote approval or a whole-run
hold. Only verified actual usage is charged; see [coverage](workflow-billing-coverage.md).
Both Start here workflows and their approved initial setup children remain included.

To pause new paid work, keep billing enabled and set the test flag false. Reads and reconciliation
remain available; existing payment and reservation records are not discarded. Startup rejects
`BILLING_ENABLED=false` against a database containing enrolled billing accounts, rather than
silently treating funded projects as unbilled. No automatic account conversion or data deletion
is performed. Before upgrading an existing billed deployment, explicitly set the new flag true.

This remains a test tariff and test-payment rollout, not live customer charging. The existing
billing eligibility limits and accounting acceptance requirements still apply.

## Welcome credits

Hosted deployments can enable `TIN_LITE_BILLING_WELCOME_CREDITS_ENABLED=true` to grant
each authenticated Tin user $10 once. The grant goes into their first joined workspace's
shared balance after project setup. It is not per login, project or workspace. Existing
Tin users receive the same grant via `scripts/grant_welcome_credits.py --apply`; without
`--apply` the script only counts users awaiting a grant. It never imports the shared Clerk
directory. Users without a project receive their grant when they join or create one.

Migration 035 separates a credit wallet from paid-run enrollment. A welcome grant alone does
not enroll the wallet; hosted defaults perform enrollment separately as described above.
Deployments without that switch may still hold welcome-only wallets. Project limits and
start-time balance checks remain authoritative; old quotes retain their historical meaning.
Workspace creators (or the sole existing workspace administrator for legacy ownerless
workspaces) administer new wallets;
joining users never gain workspace membership or billing authority from a grant.

The append-only ledger records `welcome_credit`, not a paid Stripe top-up or invoice.
Welcome credits are spent before paid funds. They do not become refundable deposits.
Refunds are handled by support through Stripe, with no dashboard, HTTP or MCP refund action.
Existing payment/webhook reconciliation and financial history remain intact.
