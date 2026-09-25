# Identity, project access, and integrations

## Current identity boundary

Clerk answers one question: **who is this person?** Tin Lite's own PlanetScale database answers the
separate question: **which projects may that person use?**

- Hosted Tin uses the same production Clerk instance as Tin Computer, so an existing person does
  not create another login. Self-host operators configure their own Clerk instance and origins.
- The dashboard, MCP, API and integration callbacks use `app.tin.computer`. The former
  `lite` hostname is retired. The optional legacy-origin setting is a migration compatibility
  mechanism, not a required or advertised service. See [the domain rollout](app-domain-rollout.md).
  Hosted Clerk production keys are scoped to
  `tin.computer` and its subdomains; the switchboard's `sslip.io` address is not an auth origin.
- Tin Lite does not read the legacy Supabase database at runtime.
- The first authenticated Tin Lite request upserts `tin_users`. That row is the explicit marker that
  this shared Clerk identity has entered Tin Lite.
- `project_memberships` is the complete authorization boundary. It deliberately has no roles.
- Any member may issue an email-bound, expiring project invitation. Acceptance adds the same flat
  membership.
- `workspaces` group projects for administration and project creation. Workspace membership does
  not grant project access, and project invitations do not grant workspace membership.
- Each project remains the content and operational ownership boundary, with its own code.storage
  repository, integrations, workflows, runs, files, decisions, chat, and Activity.

The browser API and the Clerk OAuth-protected MCP server both resolve access through those same
membership queries. Neither trusts a project ID supplied by a browser, model, or MCP client without
checking it.

The shared Clerk instance advertises Dynamic Client Registration so MCP clients that do not support
Client ID Metadata Documents can connect. Clerk requires consent for those clients and applies only
the standard `openid` scope when a client omits scopes. Tin does not use that scope as a substitute
for authorization: Tin requires an exact client ID in `TIN_LITE_MCP_OAUTH_CLIENT_IDS`, checks
returned audience/resource claims, and every MCP tool still resolves the Clerk user to a local
project membership. The same OAuth policy protects connection setup. The empty client list
disables OAuth access while preserving browser sessions; see
[client admission and rollout](clerk-agent-connection.md#oauth-client-admission). When
CIMD is generally available, prefer explicitly allowed CIMD clients and turn off public dynamic
registration if the supported client set makes that practical.

## Integration boundary

Project-owned [custom API connections](project-api-connections.md) complement the adapters
below. Their secure setup accepts selected secrets without putting values in MCP conversations
or project Files. Bounded code workflows call the trusted gateway, which resolves credentials;
this does not expose arbitrary SDK credentials to author-controlled sandbox code.

Google Search Console, GitHub, Google Workspace and Google Ads use one project-owned connection
boundary. Google Ads is the identifier-entry case: the founder enters a customer id, Tin's manager
account sends an invitation the founder accepts inside Google Ads, and no customer credential is
stored; the manager refresh token is a deployment credential. Do not port LinkedIn or another
provider as a special workflow:

1. `integration_connections` records the project, provider, external account identity, connection
   health, selected property or repository, and encrypted credential material where the provider
   requires it. Provider tokens never enter ordinary workflow state.
2. A provider adapter owns authorization, token refresh, revocation, health checks, and provider API
   translation. LinkedIn-specific behavior stops at that adapter.
   GitHub uses OAuth-on-install only to verify that the current GitHub user can access the returned
   installation ID; the short-lived user token is discarded and is never stored. Tin keeps
   only that user's numeric GitHub ID and login (`tin_user_github_identities`) so the
   [contributor check](contributing-workflows.md) can match a pull-request author to a Tin
   user. A failed lookup leaves the identity unlinked and the connection unaffected.
   GitHub issues that OAuth code only on a fresh install and does not redirect back at all
   from an already-installed app unless a Setup URL is configured, so the connection starts
   with GitHub user authorization instead of the install page. On return Tin lists the
   installations of its app the user can reach: exactly one is bound through the same access
   and write-permission checks; none returns `github_install_required` with a fresh install-page
   URL whose callback carries the code and new installation ID; several returns
   `github_installation_choice`, the dashboard asks which account, and
   `POST /api/integrations/github/authorize` remembers the chosen installation on a new
   connect attempt (migration 031) before one more authorization completes it. A callback that
   arrives with an installation ID and no code takes that same authorize path.
3. An immutable workflow definition may declare `integration_requirements`: exact provider keys,
   named capabilities, and whether each dependency is required. Catalog sync rejects unknown
   providers, unknown capabilities, duplicate providers, and extra fields.
4. Every run resolves that contract from its pinned code.storage definition commit. Tin checks the
   bound project's connection, health, selected resource, and write opt-in before Temporal starts.
   A missing requirement blocks execution with an explanation of what needs connecting.
5. The trusted switchboard gives its native activity the provider adapter, not a raw credential.
   Provider calls may link their receipt to the workflow run. Secrets never enter Temporal history,
   MCP results, generated Markdown, code.storage, or an E2B sandbox.
6. Read capabilities may run automatically. Consequential capabilities require the workflow's
   declared review and connection write permissions. A project-wide autonomy control remains
   separate future work.
7. Product activity records the human-readable effect and provider receipt, not secret material or
   a duplicate provider event log.

Before implementing LinkedIn, inspect the legacy solution for useful lifecycle rules—refresh,
revocation, rate limiting, provider receipts, and failure handling—but write the adapter against
this boundary. Do not copy its tenancy, tables, or credential plumbing wholesale.

Workflows call specific adapter operations implemented on the switchboard. A Codex procedure
can receive prepared evidence or use a grant tied to its run to request the reads its definition
allows. It does not receive the underlying GitHub or Google credential.
