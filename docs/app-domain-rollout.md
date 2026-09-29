# Domain configuration

TIN_LITE_PUBLIC_URL is the externally reachable HTTPS service origin for MCP, API,
OAuth callbacks, webhooks and sandbox services. TIN_LITE_APP_URL optionally selects
the browser origin and otherwise defaults to the public URL. Hosted Tin uses
https://app.tin.computer for both.

## Configuring a deployment

Configure DNS and TLS before selecting an origin. Register its exact sign-in/sign-up,
MCP consent, integration callback and webhook URLs with the relevant providers:

| Surface | Path |
| --- | --- |
| Sign-in / sign-up | /sign-in, /sign-up |
| MCP consent / resource | /mcp/consent, /mcp |
| Google integration callback | /integrations/callback/google |
| GitHub callback | /integrations/callback/github |
| GitHub account (OAuth App) callback | /integrations/callback/github-account |
| GitHub webhook | /webhooks/github |
| Stripe webhook | /webhooks/stripe/tin-lite |
| Twilio inbound SMS | /webhooks/twilio/sms |

Keep the configured Clerk issuer and authorized parties consistent with the browser
origins. Twilio signature validation must use the exact registered webhook URL.
Never derive service links from untrusted request Host headers.

## Migrating an existing origin

Add the new provider registrations and TLS ingress before switching runtime settings.
Drain sandbox execution and in-flight integration authentication before changing
service origin and egress policy together. Preserve identities, memberships, balances,
webhook secrets and existing provider connections.

Optional TIN_LITE_LEGACY_PUBLIC_URL accepts explicitly configured old auth returns and
ordinary browser links during a migration. It never supplies new service links. MCP,
webhooks, internal services and in-flight callbacks are not redirected. Update existing
MCP clients explicitly. The installer preserves configured app/legacy settings.

Verify discovery, authenticated MCP, auth returns, integration callbacks, webhook
signatures and a credential-free sandbox service request. Remove legacy ingress, DNS,
authorized parties and callback registrations only after old requests and leases drain.
Rollback restores the previous origin/egress configuration and provider registrations,
not stale credentials or financial data. Historical UUID namespaces are not network
addresses and must not be renamed.
