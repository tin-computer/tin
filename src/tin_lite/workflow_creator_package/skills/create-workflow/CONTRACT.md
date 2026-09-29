# Candidate contract

Return one JSON object, with no Markdown fence:

```json
{
  "format": "tin-workflow-candidate-v1",
  "files": {
    "workflow_packages/custom.example/workflow.json": "JSON manifest as a string",
    "workflow_packages/custom.example/main.py": "Python source as a string"
  },
  "qualification": {
    "version": 1,
    "assumptions": ["Which supported input sizes and branches the cases cover."],
    "cost_drivers": ["What changes the number of model calls or their input/output size."],
    "effects": ["The actual external effects, including artifact delivery."],
    "cases": [{
      "id": "ordinary",
      "description": "What this concrete case checks.",
      "inputs": {},
      "expected_status": "succeeded",
      "expect": {"contains": ["Required output text"], "excludes": []}
    }],
    "rubric": [{"id": "grounding", "question": "Does each conclusion follow from the supplied evidence?"}]
  },
  "limitations": ["Required capability or evidence still missing."]
}
```

Use the requested key consistently in the manifest and paths. files contains exactly the
manifest and every declared resource, all UTF-8 strings. No absolute paths, traversal,
symlinks, binaries, archives, imports of Tin internals or undeclared extra files. The qualifier
returns qualification.json as a separate proposed file at workflow_evals/<key>/qualification.json;
it does not belong in the runtime manifest. No test results or dollar claims belong in this format.

Cases: 1–12, unique lowercase IDs, concrete inputs without project_id (Tin binds it),
expected_status succeeded or failed. Successful cases need assertions or a rubric.
For expected failures, omit output assertions unless the contract supplies a canonical artifact.
Error diagnostics are not artifact text; check validation diagnostics in local tests.
A procedure's final message cannot set its Tin run status. For semantic input errors, prefer a
fresh diagnostic artifact with expected_status succeeded and assertions on its explicit invalid
status; do not promise a failed run just by telling Codex to stop. An existing output file is not
evidence that this run produced a result. Cases must check the supplied configuration and findings,
not only headings that an earlier report could also satisfy.
The optional expect.json_schema uses the same bounded JSON Schema subset as managed model output: closed
objects with every property required, bounded arrays, scalar types and enum; no refs or regex.
contains/excludes are literal text checks, not proof that a calculation or narrative is correct.
Keep rubric questions independent; no overall score. Maintainers review cases and the rubric.

## Runtime choices

- workflow.code: Python 3.12.8 standard library, 1–60 seconds, 1–32 files, 256 KB package,
  one UTF-8 artifact of at most 64 KB. Export run(ctx, inputs), sync or async, returning
  {"path": declared_path, "content": text}. Code has no direct network or secrets.
- Managed steps: optional code.model_routes, at most four routes and eight total calls.
  Each names provider/model/max_calls/max_input_bytes/max_output_tokens. Supported targets:
  openai/gpt-6-luna and openai/gpt-6-sol. Per-route max_calls 1–4, input bytes 1024–32000,
  output tokens 64–4096. Call await ctx.models.generate(route=..., step=..., instructions=...,
  data=..., output_schema=...). Validate result["parsed"] before use. Keep step IDs stable.
- codex.procedure: PROMPT.md plus skills/<name>/SKILL.md and declared text resources.
  Candidate resources use .md, .json, .txt, .yaml or .yml; .py files belong to workflow.code.
  SQL/Python examples may live in skill instructions for use inside the procedure sandbox.
  Private profile isolated/fenced, on_demand, bounded timeout up to 3600 seconds. One project
  artifact, or a separately reviewed GitHub PR contract. Existing model budgets remain binding.
  Choosing this executor does not grant recursion, scheduling or extra integrations.
- Approved text inputs: on-demand code packages may declare `code.evidence` with up to four
  named slots. Each has `kind: approved_output`, `input` (a UUID field), `workflow_key`
  (the allowed producer), and `max_bytes` (1–64000). Requiredness follows input_schema;
  total source text is at most 128000 bytes and serialized context at most 256000 bytes,
  including any accompanying approved article and writing guide. The slot name
  `approved_article` is reserved for the article contract. `ctx["evidence"][slot]` contains `present`,
  the selected `content`, and its run/path/revision/digest metadata; omitted optional slots
  have `present: false`. Only approved, succeeded primary text outputs from supported code
  and procedure runs qualify. Project files, unreviewed reports, binary outputs, native
  outputs, document pairs and GitHub PR receipts are not this source kind. Tin pins inputs
  before execution and reuses them on retry. Use `code.approved_article: {"input": "source_run_id"}`
  instead when the consumer needs an approved content.generate article body and original
  writing guide in `ctx["approved_article"]`. Both contracts keep model request bounds intact.
- API services: declare integration_requirements plus code.services or procedure.services.
  Up to four aliases, eight total provider calls, 16000-byte requests, and responses bounded
  to 1024–64000 bytes per alias. Use these exact byte counts, not KiB conversions.
  The byte bound, not a provider row limit, caps results: GSC search_analytics.read returns
  the rows that fit plus truncated/next_start_row, and accepts start_row and dimension_filters.
  Prefer first-party connections over custom ones when they exist: payments.stripe
  (subscriptions/customers/invoices/prices/charges.list) and analytics.posthog (query.hogql,
  event_definitions.list, property_definitions.list, insights.list). Tin projects their records
  to small fields and returns records/truncated/has_more/next_cursor; page by passing
  next_cursor as cursor in a new step. query.hogql accepts one SELECT with a final LIMIT of at
  most 1000, no OFFSET, at most 8000 bytes; Tin supplies the PostHog project, never an input.
  Code calls await ctx.services.call(service=..., step=..., operation=..., arguments=...);
  procedures use call_service with the same arguments. Operation reference:
  docs/stripe-and-posthog-connections.md.
  Custom connections: code calls await ctx.services.request(service=..., step=..., method=...,
  path=..., params=..., body=...). Procedures use request_service with the same arguments.
  Provider keys stay in Tin. GET needs http.read; POST needs http.write and the connection's
  POST permission even for a read-only query. Provider-side key scopes must restrict effects.
  Never automatically retry an uncertain request under a different step. Custom connections
  restrict origin and methods, not individual paths. Required setup belongs in the
  candidate's instructions.

Input schemas are closed objects. project_id is exactly {"type":"string","format":"uuid"}.
Tin binds and validates project_id before execution. Procedure context.inputs deliberately omits
it. Validate sandbox inputs against the client schema (remove project_id from properties and
required), or validate only the workflow-specific semantic constraints. Never demand or invent
a project_id inside procedure inputs; a provider's project ID is a separate workflow input.
Other fields are bounded strings, numbers, booleans or bounded arrays of bounded strings.
No nested input objects. Code may declare on_demand plus daily/weekly; private procedures only
on_demand. Human review is {"eligible":true,"reason":"..."} when supported, not a string.
Review after execution cannot guard an external effect that already happened.
