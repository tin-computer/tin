# Connection collection

`connections.collect` is an operator-enabled native workflow. It takes one to three
LinkedIn profile URLs, an optional keyword query and an execution policy. Each selected
profile must be a visible first-degree connection. Results are second-degree connections;
mutual first-degree connections are excluded. This integration supplies reusable collection
and export machinery. Project-specific research and delivery belong in private packages.

## Enable a project

Apply migrations through the normal deployment process. Set
`TIN_LITE_CONNECTION_COLLECTION_PROJECTS` to the designated project UUIDs and
`TIN_LITE_LINKEDIN_EXTENSION_IDS` to the allowed Chrome extension IDs, both comma-separated.
These lists default to empty. An integration appears only on an enabled project.
Membership and the allowlist are checked again on every device operation and publication.
The account owner who paired the extension must start the collection.

Load `browser-extension/` in development, then connect LinkedIn from the project's
Integrations page with LinkedIn open in that Chrome profile. The page grants a five-minute,
one-use pairing token. The worker creates its device bearer and sends only its hash during
pairing. Bearers expire after 30 days and require current project membership. Reconnecting
invalidates the older device. Migration confirms retirement of this browser’s legacy Tin
device through its existing endpoint before allowing collection. An uncertain response keeps
the legacy credential for reconciliation. It does not sign out of LinkedIn or change browser cookies.

Start the workflow through the existing dashboard or MCP run API. Use `local_only` initially.
Click **Continue collection** in the extension. It opens a collection tab, validates the
selected account and friend, applies the second-degree and keyword filters, and collects
one page at a time. Chrome must remain open and awake. The popup can close.

## Resume and limits

Each run pins its inputs and policy. Version 1 allows three friends, 20 pages and 200 people
per friend, 15 minutes per friend and 45 minutes overall. A closed browser does not reset
these limits. An account lease prevents two active runs by the same Tin account owner from
collecting that observed account across projects. This is an observed account binding,
not proof of ownership of another person's LinkedIn account.

The worker persists each batch before uploading it. Postgres acknowledges the exact batch
before navigation. Duplicate acknowledgements, including the final page, are idempotent.
A new device attempt gets a new generation and lease; stale uploads are rejected. Visible
page identity, account, friend filter and all search filters must remain consistent. Repeated
pages, ambiguous pagination, unreadable results and hidden controls do not imply completion.
The adapter currently supports English desktop pages and semantic profile/search evidence.

Results are create-only files under `connections/<run-id>/`: a manifest, CSV and bounded
JSON chunks for people and friend paths. People are deduplicated while each observed friend
path remains available. CSV values are protected against spreadsheet formulas. Coverage is
explicitly limited to visible results; limits and interruption produce partial results.
Accepted pages survive disconnect and publication failure. Canonical publication uses the
project lock, expected HEAD and the shared lost-response reconciliation helper.

## Cloud candidate and local backup

The source includes a dedicated read-only HTTP adapter. It reproduces the published
extension's cookie allowlist, user agent, language and LinkedIn request context. It does not
launch a browser, run website JavaScript, follow authentication redirects, write cookies back,
or call a logout endpoint. The extension uploads this material only after the account owner
checks the cloud-transfer option for a cloud run. Tin encrypts it using the existing
integration cipher, binds it to that run and deletes the encrypted material after finalization.
It never enters chat, MCP, Temporal history or project files.

Cloud execution requires an explicit `TIN_LITE_LINKEDIN_TEMPLATE`,
`TIN_LITE_LINKEDIN_CLOUD_QUALIFIED=true`, the integration encryption key, and a separate
`TIN_LITE_LINKEDIN_E2B_API_KEY`. Use a dedicated E2B project/account with its own capacity;
a different key in the same provider project still shares quotas. The adapter refuses the
ordinary Tin E2B key and never falls back to it. Both are off
by default. `sandbox/linkedin/` is a separate build context with a separate operator-selected
alias. Its build script refuses ordinary Tin alias names. Existing Codex, isolated code,
browser and Studio templates and their selection logic are unchanged. A separate Temporal
activity queue (`<base>-connections`, capacity 2) keeps these activities out of their slots.
Operators must provision E2B account capacity for this additional lane before enabling it.

Do not enable the qualification flag on the strength of fixture tests. Qualification must
establish a full paginated collection, matching cloud and DOM page boundaries, correct
identity, preserved normal-browser login through cleanup and later source checks, and
recovery after worker/sandbox interruption. The current GraphQL parser is deliberately
narrow: unsupported schemas stop collection. An observed query identifier comes from the
selected browser view, never from an arbitrary user-supplied request URL.

Execution policies are pinned for the run:

- `local_only`: Chrome performs the collection.
- `cloud_only`: the extension resolves the selected friends and transfers context; cloud
  failures stop or pause the run.
- `cloud_preferred`: after the same preparation, a recoverable cloud failure can continue
  in Chrome with the accepted pages, filters and cumulative limits intact. Cleanup must
  be confirmed first. The extension resumes automatically if it is awake with the correct
  account; otherwise it waits for **Continue collection**.

Challenges, rate limits, account changes and access denials pause rather than trigger a
handoff. Uncertain cleanup also pauses. No cloud retry silently creates another paid attempt.
Each page attempt has an effect receipt, bounded compute lifetime, network access restricted
to LinkedIn, root-only transfer files and usage/cleanup observations. Collection is Tin-funded
under its explicit connected-account billing policy; recorded supplier usage remains separate
from the zero customer charge. Missing supplier observations remain unconfirmed.

Cloud pagination and source-login preservation have not passed production acceptance. There
is no deployment, image build, provider configuration change or store release implicit in
installing this source.

## Verification

Use a disposable database for SQL contracts, never a configured customer database:

```sh
TIN_LITE_TEST_DATABASE_DSN=postgresql://... uv run pytest tests/test_connection_collection.py
npm ci
npm test --prefix browser-extension
npm run test:collection --prefix browser-extension
```

The Chrome tests use synthetic pages and a local fixture backend. The cloud tests use mocked
provider responses and check the separate image, request context, single purchase, cleanup,
usage receipt, fencing and backup policy. Live acceptance is a separate operator action.
