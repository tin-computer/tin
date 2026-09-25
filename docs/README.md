# Documentation

These guides are for contributing to Tin and operating your own deployment.
Internal proposals, design handoffs and production acceptance logs are archived
outside the source distribution. Current code and pinned workflow definitions
remain authoritative; older contract versions are retained for compatibility.

## Start here

- [Contributing](../CONTRIBUTING.md): local checks, integration contributions and PR review.
- [Architecture](architecture.md): execution, storage and access boundaries.
- [Feature status](feature-status.md): supported, operator-enabled and deferred capabilities.
- [Agent instructions](../AGENTS.md): implementation contracts and change safeguards.
- [Security](../SECURITY.md) and [repository policy](repository-policy.md).
- [API-only Codex execution](oauth-credential-security.md): protected execution,
  historical-run compatibility and operator verification.

## Self-hosting and operations

- [Engineering reference](engineering.md): configuration, local commands and service APIs.
- [Optional billing](self-hosted-billing.md), [billing coverage](workflow-billing-coverage.md)
  and [model accounting](model-service-accounting.md).
- [Authentication and integrations](auth-and-integrations.md),
  [MCP authentication](clerk-agent-connection.md) and [domain configuration](app-domain-rollout.md).
- [MCP onboarding handoff](onboarding-mcp.md): access, first results, delivery and partial setup.
- [Codex API execution](codex-api-pilot.md), [isolated runtime](isolated-codex-runtime.md),
  [Studio execution](studio-api-and-hosted-credits.md) and [worker lanes](activity-worker-lanes.md).
- [Publication recovery](procedure-publication-recovery.md),
  [scheduled review recovery](scheduled-review-recovery.md),
  [saved-output resolution](output-resolution.md) and [font serving](font-serving.md).

Self-hosting remains an engineering setup path. A clean-clone installation and
workflow run have not yet been verified; these guides do not imply a turnkey installer.
Deployment scripts can affect real infrastructure. Inspect and configure them for
your own accounts before use; ordinary contributor tests need no production credentials.

## Workflow and integration contributions

- [Built-in workflows](workflows.md): generated list with inputs and outputs.
- [Adding a workflow](adding-a-workflow.md) and [definition contracts](workflow-definition-foundation.md).
- [Creation and qualification](workflow-qualification.md): one creator package, versioned cases,
  static checks and explicitly budgeted evaluations using existing run evidence.
- [Public workflow packages](../workflow_packages/README.md): deterministic Python, multi-step
  managed-model Python and Codex procedures, with explicit maintainer registration.
- [Product analytics brief](product-analytics-brief.md): the public PostHog package and qualification limits.
- [Private activation](private-workflow-activation.md), [code workflows](code-workflows.md),
  [managed model steps](code-model-workflows.md) and [code schedules](code-workflow-schedules.md).
- [Project API connections](project-api-connections.md): secure credentials and external requests.
- [Stripe and PostHog connections](stripe-and-posthog-connections.md): the first-party read-only
  connections, their operations, projected records and offline test fakes.
- [Paid ads assessment](paid-ads-assessment-implementation.md): the native LLM flow that decides
  whether Google Search ads fit, its evidence sources, scorer and outputs.
- [Google Ads launch and monitor](paid-ads-launch-implementation.md): the identifier-entry
  Google Ads connection, the approval-gated launch of one Search campaign and the daily monitor
  with its automatic changes and approval-gated proposals.
- [Content programs](content-program-implementation.md), [draft generation](content-generation-implementation.md),
  [review and revisions](content-review.md) and [repository-aware delivery](repository-aware-content-delivery.md).
- [Writing style capture](writing-style-capture.md) and [editorial judgment](content-editorial-judgment.md).
- [Brand and design capture](brand-capture.md) and [reviewed document pairs](reviewed-project-documents.md).
- [Organic system execution](organic-system-execution.md) and [content continuation](organic-content-continuation.md).
- [Technical repair](technical-fix.md): finding selection, supported repairs and verification limits.
- [Diagram renderer](diagram-renderer.md), [composition checks](diagram-composition-quality.md)
  and [creative Studio](creative-studio.md).

Keep public examples synthetic. Do not add customer files, run receipts, credentials,
private screenshots or licensed font binaries to documentation or fixtures.
