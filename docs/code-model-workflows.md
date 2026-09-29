# Slice B: managed model steps in code workflows

Implemented, deployed and verified September 15, 2026, within the existing private-project pilot.
The earlier code-only [Slice A acceptance](code-workflows.md#production-acceptance--september-15-2026)
remains a separate proof of included compute and zero-credit admission.

`workflow.code` now supports optional declared model routes. Ordinary Python still owns
filtering, branching, validation and report rendering. Tin runs it independently in the same
bounded isolated runtime. Existing Codex procedures and interactive tasks keep their contracts.

## Authoring

The complete example is [code_model_example](../src/tin_lite/code_model_example/main.py), with
its [manifest](../src/tin_lite/code_model_example/workflow.json) and fixture orders. The MCP
authoring guide supplies its private copy in `model_example_files`. Use the existing commit,
validate, activate, estimate and start tools; no new tool or approval is required per workflow.
This source fixture is not an additional public Registry listing.

Add `model_routes` to the existing `code` contract:

```json
{
  "classification": {
    "provider": "openai",
    "model": "gpt-6-luna",
    "max_calls": 1,
    "max_input_bytes": 4096,
    "max_output_tokens": 512
  }
}
```

Then call the bound client from an asynchronous entrypoint:

```python
async def run(ctx, inputs):
    response = await ctx.models.generate(
        route="classification",
        step="classify_orders",
        instructions="Classify the supplied orders.",
        data=selected_orders,
        output_schema=SCHEMA,
    )
    validated_orders = response["parsed"]["orders"]
    # Validate domain rules and return the declared {path, content} report.
```

The response has `text` and `parsed`; without an output schema, `parsed` is `None`.
`ctx` retains dictionary access to `run_id` and `created_at`. A stable `step` identifies one
logical call across execution attempts. Repeating an identical completed request returns the
saved result. A different request needs a distinct step and another declared call allowance.

Current supported routes are explicitly `openai/gpt-6-luna` and `openai/gpt-6-sol`, through
the existing OpenAI adapter and pinned supplier price card. Other providers/models and custom
URLs fail validation; a configured credential alone does not admit an unpriced route.

Bounds: at most four route aliases, 1–4 calls per route and eight total; 1,024–32,000 serialized
input bytes per call (instructions, data, schema and envelope included); 64–4,096 output tokens;
64,000 response bytes. Output schemas use a small closed subset: required object fields, bounded
arrays, primitives and enums; no references or remote schema loading. The runtime retains its
60-second maximum execution window, including model wait time. Each supplier call has a
30-second limit. These limits do not promise all eight calls will fit in one execution window.

## Authority, isolation and recovery

Customer code stays in the credential-free `tin-work` UID with network access disabled.
Its Unix-socket client reaches the protected Tin launcher. The launcher passes bounded requests
through the authenticated E2B control connection to the trusted activity. It emits only a fixed
notification; user stdout and exceptions cannot issue controller commands. Requests and replies
cross protected temporary files outside the editable package directory. The controller accepts
at most 32 client messages per execution window, including cached calls.

No provider key, E2B key, storage key or bearer grant enters the customer process. No new public
gateway endpoint exists. The trusted callback binds the exact run, pinned workflow, declared
route and live sandbox lease; each dispatch and cached read rechecks project membership, active
run status, rollout eligibility, fencing and the reserved budget when hosted billing is enabled.
The existing adapter, usage recorder and billing service own the actual supplier request.

One existing Postgres `effect_receipts` row per stable step (`code_model_call_v1`) stores the
request fingerprint and bounded completed response or known validation failure. A per-run lock
serializes calls. Fingerprints include the pinned definition, route limits and complete request.
The separate existing `native_model_usage_v1` receipt records supplier intent and observed usage.
There are no model checkpoints or execution databases in project files or Temporal history.

After worker loss, Tin deletes the previous sandbox and reruns pinned code. Completed calls
recover from Postgres even if the replacement process has no model provider configured.
An uncertain call blocks further new calls, including a changed step ID; Tin never automatically
buys it again. A known schema failure remains billable and replayable as an error. Author code
may catch that error and use another declared step for a corrective request. Business-rule or
final artifact validation failures likewise preserve usage already incurred. Output publication,
stop, review, conflict recovery and projection remain the existing Slice A contracts.

Execution failures retain the failed stage in the existing output receipt: sandbox creation,
execution, checkpoint storage or result recording. The run's failure view uses that reason.
Retries keep completed model/service results and the chosen output path; a temporary checkpoint
write failure does not require another paid model call. Sandbox failures expose fixed descriptions
for timeout, package exit and result transport failure, without including authored output.

## Funding

No declared model routes means the unchanged `bounded-code-v1` policy: included compute,
zero-credit admission, no paid operation or deduction. A model-enabled definition uses
`managed-code-model-v1` and a conservative configured estimate cached by definition, inputs and
price card. This requires no paid estimation call or quote-approval step.

Hosted model runs require an enabled credit account. Existing project spending policy and
per-operation reservations enforce the pinned maximum. Tin records each observed call and
settles through the existing ledger, including usage incurred before validation failure. The
estimate ceiling rounds upward to a cent; actual usage retains the shared rounding policy.
Tiny model usage may therefore settle to $0.00 while remaining a measured, paid supplier call.
Missing usage stays unknown. No live-payment enrollment or new tariff is introduced.
Self-hosted operators with billing disabled retain server-funded model execution and usage receipts.

## Verification

Default tests use a real disposable Postgres schema, a real local Temporal server where needed,
synthetic compute/storage, and an HTTP fixture through the official OpenAI SDK and existing
adapter. They cover unsupported authority, request/schema bounds, cached estimates, zero-credit
and unenrolled rejection, revoked/stale authority, immutable step fingerprints, call limits,
unknown attempts, restart replay, and paid usage after schema/artifact rejection. A mocked Astra
response settles $0.02 exactly once across worker restart and repeated settlement.
