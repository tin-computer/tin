# Isolated Codex procedure runtime

## In plain English

The Codex controller holds a temporary run-bound API relay grant. Workflow commands
run as a different user that cannot read that grant or replace controller programs.
The provider key stays on the switchboard. Pooled ChatGPT authentication is retired.

## Contract

- A pinned procedure may select `sandbox.profile: isolated` with fenced egress and the
  existing 1–7200-second timeout contract (1–3600 before October 2, 2026). The switchboard must explicitly configure
  `TIN_LITE_E2B_ISOLATED_TEMPLATE`; no missing-image fallback to the default is allowed.
- The separate `tin-lite-codex-isolated` image pins Codex CLI 0.156.1 and `gpt-6-sol`.
  The controller uses Codex's native experimental exec-server environment to run command,
  patch, and image tools as the `tin-work` UID. Local fallback environments are not selected.
  Project configuration cannot start local MCP servers or hooks; controller configuration
  and its API relay grant live outside the author-readable directories.
- The local exec-server connection bypasses the external proxy only for loopback addresses.
  The controller checks the environment's native readiness before starting a model turn;
  provider traffic still uses Tin's proxy and network fence. Both the feature flag and the
  model-aware agent setting disable delegation to additional agents in this profile.
- Commands and verification receive a clean environment, no additional capabilities, and
  Linux `no_new_privs`. Provider keys, model/storage grants, reusable proxy credentials,
  and the controller's run-tool grant do not enter their environment. The ordinary trusted
  controller may still use Tin's declared, run-bound MCP integration gateway.
- The image provider makes `/usr/local` world-writable during finalization. Runtime
  preparation removes group/other write permission there before any author process starts.
  The image-protocol check runs before the runner starts API execution.
- Git metadata remains controller-owned. A sticky workspace root prevents its replacement.
  Verification runs as the author UID, with its output excluded from the control stream.
  All author processes, including detached children, are stopped and killed before Tin
  reads/stages the result. The frozen tree rejects symlinks, hard links, and special files.
  After every workspace validates, ownership returns to the controller with owner read/write
  (and directory traversal) restored. This supports nested 0700 directories and 0600 files
  created under the runner's private umask without making credentials readable by the worker.
- Existing project-artifact validation, fenced ephemeral checkpoints, conflict preservation,
  review eligibility, and the project-owned GitHub PR gateway remain the delivery contracts.
  This profile does not merge PRs or grant integrations that the project did not authorize.

## Observations and limits

Only the protected controller's `thread/tokenUsage/updated` stream supplies Codex usage.
Counts are bound to the exact thread/turn, checked, monotonic, and deduplicated as cumulative
totals. Workspace rollouts, stdout from commands, and result-file claims are not accounting.

At 250,000 observed cumulative tokens the controller interrupts the turn and refuses output
delivery. This is an observed stop threshold, **not a guaranteed spending ceiling**. A
provider request already accepted can finish after interruption. Existing E2B timeouts and
the shared HTTP/MCP Stop operation remain the compute/cancellation controls.

One `isolated_codex_attempt_v1` effect receipt records intent before execution, then protected
usage observations and elapsed host time. It reuses the owning activity's SQL connection.
Cancellation, rejected output, and lost responses preserve prior observations; missing usage
and supplier cost remain unknown, not zero. Elapsed time is not an E2B invoice. No billing
table, price estimate, or retail charge is introduced.

Durable checkpoint recovery happens first. If an attempt exists without recoverable output,
automatic retry cannot purchase that model execution again. Publication reconciliation and
already-retained output use the existing recovery path rather than rerunning Codex.

## Verification and remaining acceptance

Automated coverage includes real disposable Postgres receipts, duplicate prevention, usage
on cancellation/rejected output, malformed counters, and frozen-tree rejection. Opt-in real
E2B tests run the actual pinned app-server/exec-server and Tin bridge against synthetic
Responses streams. They check credential/runtime/Git protection, native patch/image tools,
local-environment rejection, project MCP configuration, successful artifact plus Git commit,
verification failure, malformed final output, token interruption, detached-process cleanup,
and sandbox deletion. These synthetic tests spend compute but make no paid model request.

Run them against a built image with:

```sh
TIN_LITE_LIVE_CODEX_ISOLATION_TEST=1 \
TIN_LITE_TEST_ISOLATED_TEMPLATE=tin-lite-codex-isolated \
uv run pytest tests/test_codex_isolation.py -k live
```

Pooled OAuth and its refresh probe are removed. See
[API-only execution](oauth-credential-security.md) for the current contract and rollout.
