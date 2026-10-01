# Public ChatGPT plugin

The public plugin exposes existing Tin projects through `/mcp/plugins`. It uses the
same Clerk verifier, workflow registry, start service, review services, project access
checks and billing ledger as the coding-agent endpoint. It does not create a second
workflow engine or a separate account system.

This is source support, not a deployed or approved directory listing. The upload source
is [plugins/tin-mcp](../plugins/tin-mcp). Do not submit it before the live checks below.

## Implementation and release steps

| Step | Scope | Source and remaining verification |
| --- | --- | --- |
| 1. Define the public surface | Existing accounts and projects; explicitly selected native and published package workflows; existing connections and balances. No account/project creation, onboarding, arbitrary tasks, private packages, connection setup or checkout. | `PublicMCPExposure` on both existing catalog sources and `SHARED_TOOLS` in `mcp_public.py`. New internal tools and workflows are excluded by default. |
| 2. Isolate OAuth and routing | Dedicated `/mcp/plugins` resource and discovery document, using the configured public origin and existing Clerk issuer. | Shared `ClerkAuth` implementation, separate verifier instances/caches. Public resource rejects legacy unbound tokens. Run real Clerk PKCE/resource/refresh acceptance after deployment. |
| 3. Expose workflow starts | One named tool per selected native or published package workflow, generated from its canonical input schema. Support new inputs or one saved configuration. | Both forms delegate to the existing MCP start handlers and `start_workflow_run`. Check live discovery after catalog synchronization. |
| 4. Preserve workflow controls | Status, files, output, charges, exact-version reviews and existing stop operations. | Shared handlers, with public run eligibility and project membership checked before dispatch. Existing review, funding, prerequisite, effect-receipt and retry contracts remain authoritative. |
| 5. Publish reviewable metadata | Explicit read-only/destructive/open-world hints, OAuth declarations, bounded arguments and public response fields. | Metadata and HTTP regression coverage; portal tool scans and actual ChatGPT behavior remain separate checks. |
| 6. Verify and document | Focused regression tests, normal full-suite CI, catalog generation and import boundaries. Optional domain challenge route. | `tests/test_mcp_public.py`; existing auth, review and billing suites. Fixtures do not establish live provider acceptance. |
| 7. Prepare the submission | Listing, icons, five positive and three negative cases, review access, demo and package. | Source metadata and packaging command are provided. A real demo, deployed endpoint, reviewer access, publisher/domain verification and portal checks remain required. |

## Endpoint and authentication

The resource is exactly `TIN_LITE_PUBLIC_URL` plus `/mcp/plugins`. For the documented
hosted origin this is `https://app.tin.computer/mcp/plugins`. The corresponding metadata
URL is `/.well-known/oauth-protected-resource/mcp/plugins`. Never derive either from
an incoming Host header. The old `/mcp` endpoint and its client policy are unchanged.

Clients must request the public resource during authorization and token exchange.
Tin accepts only Clerk-introspected or signature-verified matching resource claims;
`TIN_LITE_MCP_OAUTH_CLIENT_IDS` does not admit unbound tokens to the public endpoint.
Separate verifier instances prevent success-cache entries from crossing the resources.
A token explicitly authorized for both resources can be used at both, as usual.
Project membership is checked on every operation, independently of token caching.

Use Clerk's existing PKCE S256, consent, registration, issuer and callback behavior.
Configure the [ChatGPT client scopes and PKCE policy](clerk-agent-connection.md#client-scopes-and-pkce)
before connecting. ChatGPT requests Clerk's advertised `openid`, `email` and `profile`
identity scopes; an `openid`-only client registration fails before sign-in. Changing
dynamic-registration defaults alone does not repair an existing client.
No provider settings are changed automatically by the server. Verify a fresh client,
the exact audience, expiry, refresh,
revocation, wrong-resource rejection and denied-project access in the development
connection before public submission. A new resource needs a fresh authorization.

Authentication acceptance must cover each layer:

- Protected-resource discovery identifies the exact public endpoint and Clerk issuer.
- Both Clerk discovery documents advertise accurate endpoints, OIDC scopes, token
  authentication methods and `S256`; every requested scope is allowed on the client.
- The existing connection and a fresh registration reach sign-in with
  `scope=openid email profile`, the exact resource, callback, state and S256 challenge.
  Missing PKCE and `plain` challenges are rejected. An authorization redirect reaching
  sign-in is only a pre-login check, not a completed connection.
- A real user completes consent, ChatGPT exchanges the code and can list tools and
  existing projects. Confirm the issuer on callbacks, the resource audience on issued
  access tokens, refresh, expiry and revocation without logging tokens or codes.
- The signed-in connection starts one authorized test workflow, reads its result and
  rejects inaccessible projects and tools excluded from the public surface. A working
  coding-agent `/mcp` connection does not establish `/mcp/plugins` acceptance.

Clerk's OIDC UserInfo endpoint must return `email` and `email_verified: true` for a
verified identity to support ChatGPT Enterprise workspace domain restrictions. Check
this through the normal authorized connection; do not invent verification claims.

OAuth declarations are carried on every tool in `_meta.securitySchemes`, the documented
compatibility location supported by the pinned Python MCP SDK. The endpoint also
advertises protected-resource metadata and challenges HTTP requests. Tool-level
unauthenticated results include `_meta["mcp/www_authenticate"]`. Verify the imported
per-tool auth policy in the portal rather than assuming a successful ZIP import proves it.

Listing existing projects never provisions a project, records a new Tin user, grants
credits or sends a welcome email. Sign-in and consent remain Clerk-owned. An account
with no projects receives an empty list. Existing account-management routes outside
this plugin retain their normal behavior.

## Tools and execution

`BuiltinWorkflow.public_mcp` and `PublicWorkflow.public_mcp` share the same presentation
metadata: a stable public tool name and effect hints. This metadata is not serialized into workflow definitions and does not change immutable revisions.
Input schemas come from `client_input_schema(entry.definition)`; the project is bound
outside workflow inputs by the shared run service. The public tool inventory combines
`BUILTIN_WORKFLOWS` and `PUBLIC_WORKFLOWS`:
29 native workflows plus 18 reviewed published packages, for 47 named starts.
Package schemas are read through the same safe manifest decoder used by catalog
publication. No package code runs during discovery, and unregistered folders,
`example.*` packages and private `custom.*` workflows are not exposed.

Within the reviewed scope, four catalog entries lack a standalone start: `growth.onboarding`,
`growth.onboarding_plan` and `project.task` are outside this plugin's scope;
`content.deliver` is invoked through the existing approval/delivery flow. New entries
still require explicit reviewed presentation metadata; registration does not silently
expand the public callable surface.

Subsequent catalog additions remain outside this release's reviewed tool set:
`website.change`, `content.refresh`, `social.x_revise`, `social.x_draft`,
`social.x_style`, `social.x_publish`, `social.x_compose`, `competitor.sunset_rescue`,
`growth.framework_starter` and the organic loop's five packages (`organic.traffic_snapshot`,
`organic.content_efficacy`, `organic.site_architecture`, `content.blog_index` and
`organic.prompt_panel`). They keep their ordinary Tin registration. Adding
ChatGPT tools for them requires an explicit review of their effects and controls.

Every start takes a project UUID and required request UUID. Choose exactly one form:

```json
{
  "project_id": "00000000-0000-4000-8000-000000000001",
  "request_id": "00000000-0000-4000-8000-000000000002",
  "inputs": {}
}
```

This example fits `start_project_memory`. Other tools declare their own inputs.
For a saved configuration, replace `inputs` with `project_workflow_id`. The saved
configuration must belong to the selected project and the tool's fixed workflow.
Its input schema must match the public contract, and its existing revision stays pinned.
A new start pins the current definition; retrying its request ID preserves the original
run's revision.

Before public deployment, audit the immutable revisions used by eligible saved
configurations against each tool's advertised effects, integrations and approval rules.
An equal input schema establishes argument compatibility, not behavioral equivalence.
The shared resolver loads the whole pinned definition, including its review policy and
resources; it does not replace older behavior with the current catalog's behavior.
Historical definitions in the deployed Registry are not all available in this source
checkout. Live revision compatibility therefore remains a release check. If a revision
falls outside the reviewed public contract, exclude it explicitly from public execution
before release; never silently move a saved configuration to a newer revision.

Style capture takes an existing, explicitly user-selected sample packet in project Files.
For technical repair, use `list_technical_fix_sources`, `get_technical_fix_source` and
`preflight_technical_fix` to inspect the exact audit, selected findings and any judgment calls; the
ordinary run service still verifies the selection before execution.

There is no generic `start_workflow` escape hatch, client-selected
executor or dynamic schema-discovery/execution pair.

The same admission service checks prerequisites, integrations, provider eligibility,
funding and limits. Missing prerequisites do not invoke hidden onboarding tools.
Errors explain unavailable existing funds or project readiness without returning a
checkout link or internal suggested tool call. Normal Tin rates apply; no special
ChatGPT surcharge or quote-approval loop is added.

Public registration excludes tools outside the explicit shared allowlist. Dispatch
also rejects unknown tool names, so knowing the name of `create_billing_checkout`
does not make it callable. Run operations reject private, onboarding and arbitrary-task
runs before reaching the shared handler. Workflow lists omit unsupported entries.
Project files remain ordinary project-authorized content; this is not a separate file
permission system and does not conceal files produced by other workflows.

Results omit internal onboarding handoffs, generic next-call instructions, billing
administration, telemetry and supplier rate cards. Document text is preserved as data.
File history keeps revisions, authors, dates, states and user commit messages, removing
only the trailing internal project-file request marker from public responses.
The public server omits the coding-agent PostHog MCP instrumentation; existing product
usage/audit recording and workflow accounting still apply. Do not describe it as
collecting no operational metadata.

### Effects and annotations

Read-only project/file/list/charge tools are closed-world. `get_run` is marked mutable
and open-world because it can refresh a persisted public-page probe. Starts and review
changes are writes: they can replace artifacts or authorize irreversible work. Stops
cancel ongoing work and retain previously saved output; they never promise to undo
sent email, provider requests or already-created repository changes.

Effect hints are explicit for each catalog entry. Internet research, public sites,
recipients and open-ended external targets are open-world; bounded project memory,
repository reads, weekly project briefs and local diagrams are not open-world merely
because their execution is hosted. Changes to a workflow's capabilities require a
review of its metadata before the public endpoint is deployed again.

Starts can have consequential effects beyond producing a report: the organic system
can create a weekly drafting schedule; site repair can open a PR; outreach can send
approved email; Ads workflows can enable a reviewed campaign and make bounded changes
in an existing advertiser account. Normal backend approval requirements still apply.
A generic annotation is not a substitute for explaining the selected workflow's effects
and obtaining the user's authorization. Paid-ads proposal management and other controls
not on the public allowlist remain in Tin.

Article and document revisions use the existing exact `review_token` and request-ID
contracts. Approval with repository delivery may start a separately metered adaptation.
`github_pr` leaves an unmerged PR; `github_commit` can write or merge into the default
branch under the existing delivery contract. Website deployment is not implied.

## Listing and upload source

The confirmed publisher name is **Emotion Machine Inc**. The portal's selected verified
identity must match; package fields cannot verify a business or select an organization.
The user requested all available countries, represented by `publication.countries: []`.
The listing uses Tin's existing icon, rendered from `static/tin-favicon.svg`; no dark-mode
variants or additional brand colors are included.

The website, privacy and terms URLs point to Tin's published pages. The support URL is
`https://github.com/tin-computer/tin/issues`, the publisher-confirmed support channel.
The page is public; GitHub sign-in is needed to file an issue. Before submission, confirm
that the published privacy policy covers the public plugin's actual data flows,
retention, service providers, usage recording and deletion practices. Do not invent
new legal commitments in the package.

Run the offline source checks:

```bash
uv run python scripts/package_public_plugin.py
```

The five positive and three negative cases are in
`extensions.com.openai.review.test_cases`, not in a separate unimported checklist.
They cover project/saved-workflow discovery, file search, an ads launch plan held for approval, a pinned saved
start and an exact-version revision; negative cases cover onboarding, checkout and
private execution. These are drafted host-level cases, not evidence that ChatGPT has
already passed them. Use synthetic fixtures and record actual outcomes before review.

After the demo URL has been finalized, and the public endpoint
is deployed and verified, build outside the source directory:

```bash
uv run python scripts/package_public_plugin.py --output /tmp/tin-mcp-0.1.0.zip
```

The command checks live discovery, then includes only `plugin.json`, `mcp.json` and the
icon in a single `tin-mcp/` directory. No `.app.json`, app bindings, source code,
credentials, review-account instructions or dependency trees are included. It does not
upload, connect, submit, publish or attest to compliance. Portal validation and live
case results remain required even when the archive's structural checks pass.

### Commerce review

Existing Tin balances fund normal usage; no credit purchase, upgrade or checkout tool
is exposed. The publisher explicitly selected Google Ads launch and monitor for the
public scope. The review metadata therefore declares `commerce: true` and explains
that a reviewed campaign can incur separate Google Ads account charges. This flags
external spending for review; it does not claim that Tin sells credits in ChatGPT.

The guidelines allow access to existing paid-account features but prohibit selling
digital products/services. That allowance does not establish approval of every
advertising or external spending operation. OpenAI must review these actual
advertiser-account capabilities before publication. Do not describe them as read-only,
cost-free, or already policy-approved. If review requires a narrower scope, remove the
relevant catalog exposure and public controls while preserving Tin's normal workflows.

## Review account and demo

Provision a dedicated test account outside ChatGPT with a synthetic **Plugin review**
project, pre-existing connections, appropriate permissions and test funding. Do not use
customer content or live paid services as implicit test setup. The account must be usable
without the reviewer's access to your mailbox, phone, MFA device or private network.
Put credentials and precise login steps only in the portal's secure reviewer fields.

Use a development connection in ChatGPT to the deployed public endpoint before recording.
A localhost fixture test or script is not a ChatGPT demo. Rehearse these interactions:

1. Show Tin's version and the existing project selection; keep sign-in secrets off screen.
2. Ask “Show my existing Tin projects and the saved workflows in Plugin review.”
3. Ask to find and summarize the synthetic sample launch notes; show the file-backed result.
4. Start project memory or the saved weekly brief; show the real run ID, then read status
   and a real completed output. Never simulate completion or silently buy test credits.
5. With the dedicated advertiser fixture ready, ask for an ads launch plan from its
   completed assessment. Show the real plan and budget while leaving approval pending;
   do not enable a campaign as an implicit demo step.
6. Read the prepared article review, request a shorter opening, and show the actual new
   revision awaiting approval. Leave delivery set to none for this synthetic case.
7. Ask to buy credits or execute a private workflow; show the truthful capability limit.

Record with a screen recorder while using the actual development connection. Replay the
video to verify readable prompts and results, no secrets, and the required behavior.
Host it at a reviewer-accessible URL, verify playback without your private session, and
set `review.demo_recording_url`. No recording or playback verification is claimed by the
source package. Run all eight submitted cases against the saved portal version as well.

For domain verification, configure the portal's exact challenge text in
`TIN_LITE_OPENAI_APPS_CHALLENGE` on the endpoint's deployment. The optional
`/.well-known/openai-apps-challenge` route serves it as plain text with no appended newline;
without configuration it returns 404. Setting it and deploying are separate operator
steps. Keep developer verification, connection, scans, legal attestations, submission
and publication distinct; an approved review does not itself publish the plugin.

## Official references

Requirements were checked against the OpenAI documentation on October 1, 2026:

- [Plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines): existing
  paid-account access, no digital checkout/upsell, independently reviewable operations,
  data minimization and accurate effect hints.
- [Authentication](https://developers.openai.com/plugins/build/auth): OAuth resources,
  PKCE, per-tool auth policy and runtime challenges.
- [Tool reference](https://developers.openai.com/plugins/reference): annotations and the
  `_meta.securitySchemes` compatibility field.
- [Submission](https://developers.openai.com/plugins/deploy/submission): portable package,
  listing and review fields, cases, countries, scans and publication.
- [App review](https://developers.openai.com/plugins/deploy/app-review): reviewer access,
  evidence and review readiness.

The portal's current requirements and review outcome remain authoritative. Structural
schema validation does not establish policy approval or live authentication acceptance.
