# Tin's coding-agent connection

Tin uses Clerk as its OAuth authority and gives sign-in, verification and consent Tin's
Paper 25–27 presentation. Authentication does not create a workflow or approve a plan.
After connecting, the founder returns to their project and asks their coding agent:
“Use Tin to grow my project like a pro!” The existing `get_started` contract guides the
codebase read, founder questions, plan and explicit approval.

## Protocol and return path

The dashboard, MCP resource, Clerk consent and integration callbacks all use
`app.tin.computer`. Existing coding-agent connections should update to
`https://app.tin.computer/mcp` and reconnect. The old host is only temporary inbound
compatibility. See [the domain rollout](app-domain-rollout.md).

`/.well-known/oauth-protected-resource/mcp` continues to advertise
`https://clerk.tin.computer`. Clerk's own discovery, authorization, registration, token
endpoint and callback issuer remain authoritative. Do not point discovery at Tin or
rewrite the issuer. The previous rewritten discovery routes return 410; legacy
`/mcp/authorize` links redirect to Clerk with their original query.

Clerk sends a signed-out person to the configured sign-in page with `redirect_url`.
Tin validates and HTML-escapes that address on the server and gives the exact address
to Clerk on both sign-in and sign-up, including when switching between them. An already
signed-in person resumes it before any product bootstrap. Ordinary product invitations
and integration callbacks retain their existing destinations.

Accepted destinations are this product, the shared parent Tin site, Clerk's exact
`/oauth/authorize` and `/oauth/authorize/continue` routes, and the hosted
`accounts.<domain>/oauth-consent` path for in-flight connections during rollout/rollback.
Other origins, non-HTTPS URLs, credentials, fragments, controls, duplicate return
parameters and sign-in loops are rejected. Clerk independently validates the actual
OAuth client's callback and request.

`/mcp/consent` mounts Clerk's `OAuthConsent` with shared Tin appearance settings. Clerk
owns the requesting client's identity, scopes, redirect disclosure, optional organization
selection, Allow/Deny buttons and form submission. Tin never auto-consents, brokers tokens,
reads a saved client token, or implements its own OTP. A failed load offers reload and a
fresh connection from the coding agent. No third-party analytics runs on this page.

## OAuth resource binding

New clients use the standard sign-in → Clerk consent → connected journey. No Tin
administrator needs to approve each dynamic registration. Clients must request the exact
resource advertised by Tin: `TIN_LITE_PUBLIC_URL` plus `/mcp`. Registration, a client name,
generic `openid` consent and project membership alone do not authorize access to Tin.

In Clerk's OAuth application settings, enable JWT access tokens and resource audiences
(`oauth_jwt_access_tokens: true`, `aud_claim_enabled: true`). The documented Backend API
is `PATCH /v1/instance/oauth_application_settings`; change only the necessary fields.
Keep the issuer, existing sign-in/signup/consent pages, default `openid` scope and
supported-client registration settings unchanged. These are shared-instance settings:
check other resource services before changing them.

Session authentication still uses Clerk's signature and origin verification, then requires
the verified session ID and rejects access-token JWT types. OAuth JWTs cannot acquire
session authority by passing through the connection helper's browser fallback. Regression
coverage exercises the real pinned SDK with ephemeral signed tokens and both bearer/cookie
transport; backend-minted session continuations retain their existing absent-origin handling.

Clerk's authenticated verification endpoint establishes token validity, client identity,
`openid`, and unrevoked/unexpired state. It may omit the audience even when the JWT has
one. Tin therefore also verifies the JWT signature against the configured Clerk instance's
key, its issuer and expiry, and its subject/client agreement with introspection. It never
trusts a decoded-but-unverified audience or a token-supplied key URL. Every resource claim
in either source must include Tin's exact resource. Conflicts and malformed claims fail
closed. The expected resource never comes from Host, app-navigation or legacy origins.

MCP runs stateless HTTP, so the shared verifier keeps a small in-process memory of
completed successful verifications, keyed by the token's SHA-256 digest (the raw token is
not stored). An entry lasts at most 60 seconds and never past the verified expiry;
rejections and upstream errors are never remembered. A revocation at Clerk therefore takes
effect within 60 seconds. Project membership is still checked on every request.

For legacy clients that cannot obtain resource-bound tokens, an operator may explicitly
authorize exact, case-sensitive IDs in `TIN_LITE_MCP_OAUTH_CLIENT_IDS`. This optional
comma-separated policy defaults empty and is not needed for ordinary new connections.
It authorizes that client's otherwise-valid, unbound `openid` access tokens from this
Clerk instance; never add unrelated SSO clients, names, wildcards or inferred historical
clients. It cannot override a wrong resource, invalid JWT signature, revocation or expiry.
The installer preserves this optional policy; restart after changing it. Removing a
legacy ID removes that exception, not access via properly resource-bound tokens.

Existing unbound tokens may require one reconnect after enabling audiences. Do not
grandfather all DCR clients to avoid reauthentication. Dashboard sessions, accounts,
project provisioning, invitations, welcome credits and stored workflows are unchanged.
Verify a fresh DCR client can complete normal consent and call Tin without operator
enrollment; wrong-resource and unbound unrelated tokens must fail. Also verify revocation,
token refresh, project membership, and the existing dashboard/signup return paths.
Fixture coverage is separate from a completed real-client production connection.

References: [Clerk token verification](https://clerk.com/docs/guides/configure/auth-strategies/oauth/verify-oauth-tokens),
[verification response schema](https://clerk.com/docs/reference/backend-api/2026-05-12/tag/oauth-access-tokens/POST/oauth_applications/access_tokens/verify),
[OAuth settings schema](https://github.com/clerk/openapi-specs/blob/main/bapi/2021-02-05.yml),
[Clerk CIMD management](https://clerk.com/docs/guides/configure/auth-strategies/oauth/client-id-metadata-documents).

## Production activation

Deploy and verify the code before changing Clerk. These are **shared instance** settings
for Tin Computer and Tin Lite. In Clerk Dashboard → Configure → Paths, record the old
values and change these locations in order:

1. OAuth consent: `https://app.tin.computer/mcp/consent`.
2. Sign-in: `https://app.tin.computer/sign-in`.
3. Sign-up: `https://app.tin.computer/sign-up`.

Keep the existing home and default after-sign-in/after-sign-up locations. Explicit parent
Tin return URLs are preserved. Existing deployed apps that set their own auth URLs should
be checked separately; shared Paths cannot override an app's explicit SDK configuration.

In Configure → User & authentication, align signup with the accepted email-code design:
remove the password requirement and disable password signup if Clerk keeps presenting the
optional password field. Keep email required, verification at signup, email-code sign-in,
Google and the existing bot protection. Do not delete existing passwords, users, sessions,
projects or grants. On September 14, the public environment showed email-code sign-in
already enabled, **password required at signup**, and no password sign-in factor. This
explains why the actual signup component still showed a password despite the Paper design.

Keep OAuth consent enabled and the existing `openid` default scope. The path settings
and signup requirements are not exposed by Clerk's documented Backend API; use the
signed-in admin dashboard, not a private dashboard API or exported browser cookies.

Before declaring the journey live, check in the founder's normal client/browser:

- Fresh signed-out connection → Tin sign-in, switch to signup and back without losing
  the return; Google and email verification both resume the connection.
- Existing signed-in connection → Tin consent directly, without an extra personal project.
- Consent shows the requesting app, signed-in identity, scopes and callback destination;
  Deny returns cancellation, Allow completes the native client's OAuth login with Clerk's
  issuer. A reused grant may legitimately skip consent.
- Back in the project, use the prompt above and confirm native Tin tools are available.
  If the current agent session has not loaded them, restart the session. Authentication
  alone cannot make a coding agent start a conversation or execute work.
- Direct Tin sign-in, invitation acceptance, integration callbacks and explicit returns
  to the parent Tin website still work.

Rollback: restore sign-in and sign-up to `https://accounts.tin.computer/sign-in` and
`https://accounts.tin.computer/sign-up`, then restore consent to
`https://accounts.tin.computer/oauth-consent`. Restore any separately changed signup
requirement to its recorded value if needed. Keep Clerk discovery advertised; never roll
back to rewritten Tin issuer metadata.

## Verification and concurrent work

Backend tests cover exact return preservation, escaping, rejected destinations, retired
discovery and legacy redirects. Packaged browser tests cover both auth modes, existing
sessions, consent mount boundaries, SDK failure, paper/coal, mobile layout and visible
Allow/Deny controls. Consent data in those tests is synthetic; it is not a live grant.
The real production ClerkJS/UI also rendered both signed-out forms locally without
creating an account or sending a verification email. Auth font loading and layout were
checked using the actual packaged font files.

A read-only production authorization probe using an existing Codex client's public
configuration confirmed Clerk's `/oauth/authorize` → `/oauth/authorize/continue` →
configured sign-in redirect and preservation of state, PKCE, resource and callback fields.
This does not prove a completed login; the normal-client acceptance above remains required.

Emre's open PR #34 contains a similar post-copy prompt, an opening offer and optional
welcome email. This change carries the post-copy handoff with repeated-copy and clipboard
failure handling; rebase that overlapping hunk before merging #34. Its mail runs on the
first MCP tool call that inserts `tin_users`, not at OAuth completion, so it cannot recover
someone who never reaches their first tool call. Email is not required or enabled by
this connection fix.

References: [Clerk custom consent](https://clerk.com/docs/nextjs/guides/configure/auth-strategies/oauth/custom-consent-page),
[Clerk redirect options](https://clerk.com/docs/js-frontend/reference/objects/clerk),
[Clerk Backend API schema](https://github.com/clerk/openapi-specs/blob/main/bapi/2021-02-05.yml).
