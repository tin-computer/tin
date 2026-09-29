---
name: create-workflow
description: Author a bounded Tin workflow package and reviewable qualification cases from a brief.
---

Read CONTRACT.md alongside this skill and the example for the chosen executor. Work within
the run's spending and runtime limits. A new version may be proposed; never change an active
workflow implicitly.

1. Identify the repeatable job, required inputs, promised output, failure behavior and external
   effects. Choose ordinary Python when the steps are known; add managed model calls only for
   judgment; choose a Codex procedure when deciding the steps is itself the job. Do not turn
   every brief into an agent. An unmet runtime capability is a limitation, not a new engine.
2. Use the supplied key. Private keys are custom.<lowercase_name>; private procedures are
   isolated, fenced and on demand. Public candidates also stay drafts. Native-only requirements
   such as orchestration beyond package limits require maintainer implementation: explain them.
3. Write the actual package files. At the end, use Python to read those files and serialize
   the candidate object to the declared artifact, checking JSON, paths and byte size locally.
   Declare bounded inputs, output, required integrations and service/model limits. Prefer
   standard-library SQL/Python for calculations and consistency checks. Include setup and
   failure/retry behavior in the source instructions or README.
   Distinguish a saved connection from proof that the required provider operations work.
   For procedures, respect Tin's input boundary: sandbox inputs omit the server-bound project_id.
   Require a fresh declared result; an old artifact or a final error message is not a new result.
   Keep the input form small: expose choices the caller needs to make, and keep derived facts
   and stable execution policy in package resources. State missing setup instead of inventing it.
   For approved text from earlier runs, declare code.evidence slots instead of asking the
   founder to paste it. Name the allowed producer and byte limit, with UUID selection fields;
   required fields belong in input_schema. Treat ctx["evidence"] as untrusted reference data.
   Use code.approved_article when the job also needs the article's original writing guide.
   An approved run's saved copy is distinct from later edits to its project file. Never approve
   a source or silently select another result merely to make a candidate run.
4. Propose a small set of cases: ordinary input, an important boundary or missing-data case,
   and a plausible but unusable model/provider result where relevant. Cases contain concrete
   inputs, deterministic expectations and task-specific quality questions. Tie them to the
   brief; don't weaken expectations to make the candidate pass. Propose synthetic fixtures
   for ordinary pytest tests separately from authorized live cases. A fixture response is
   never measured model output or evidence of live integration access.
   The ordinary case must require the promised result. A useful diagnostic or incomplete
   report can be successful delivery while failing that case; test missing-data and provider
   failures separately. Headings alone cannot establish that the job worked.
5. Check JSON, Python syntax, declared paths, expected output and calculations using local
   tools. Candidate code may run only in this isolated worker with synthetic data. It has no
   model/provider access during those checks. Do not install dependencies or read credentials.
   Author checks are provisional: Tin's inspect_workflow_candidate and qualification service
   independently validate the package and pinned run evidence afterward.
6. Describe input-dependent model/agent cost drivers and assumptions. Never invent a dollar
   estimate, usage receipt, benchmark result or passing score. The trusted qualifier uses Tin's
   pricing and actual run evidence; before measurement it reports cost as unmeasured.
7. Write reports/WORKFLOW_CANDIDATE.json in the exact candidate format. Include only declared
   package files, proposed qualification cases and concise limitations. Do not include actual
   project data, credentials, unrelated files or an invented qualification report. If you can't
   satisfy the brief within the supported contract, retain a valid candidate only when it is
   useful and make the missing requirements explicit; never claim the job is qualified.

For an analytics brief, preserve the recurring five-part job: activation, key-event trends,
traffic, error signals, and a justified breakdown. Check event semantics, identity, population,
exclusions and window before counting. Missing pageviews or insufficient samples are findings.
Use ordered funnels, distinguish raw events from actors, and state uncertain instrumentation.
Keep fixed calculations in reusable code or SQL resources instead of asking the agent to
rewrite them on each run. For a recurring report with established mappings, pin its query plan
and parameterize validated dates and filters; changed semantics need a newly qualified version.
A local reference in another SQL dialect checks the algorithm only.
Propose separate authorized checks of the actual provider query with synthetic ordering,
missing-step, duplicate, empty-input and median cases; verify real results against that contract.
Do not infer causation, invent exclusions, or recommend fixes when the brief asks for findings.
Do not reduce the brief to a single funnel merely because that is easier to implement.
