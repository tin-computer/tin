# Agent harnesses

A `codex.procedure` runs on Codex unless its definition names another harness. Tin
picks the harness and model for each procedure, in its definition:

```json
"procedure": {
  "agent": {"harness": "antigravity", "model": "gemini-4-neon", "effort": "high"}
}
```

`effort` is `low`, `medium`, `high` (default) or `xhigh`. Without `agent`, nothing
changes: Codex, its contracts, relay, rate cards and controller run as before. A run pins
the harness, its version, the model and effort it was admitted with. Changing a
procedure's agent is a definition change, so it affects future runs only.

The harnesses share everything around the agent: admission, the session budget and
per-workflow ceilings, receipts, the isolated sandbox and its egress fence, the
project-state checkout, connected repositories, `tin-run` tools, output checks, delivery,
review and recovery. The package itself (`PROMPT.md` and `skills/<name>/SKILL.md`) is the
same for every harness.

## Antigravity on Gemini 4 Neon

[Antigravity](https://antigravity.google/docs/cli/headless/) is Google's agent harness
for Gemini. Neon scores far higher inside it than in a minimal harness, so Tin runs
Google's harness rather than its own loop.

- **Image.** The isolated image carries `agy` 1.5.16, installed from Google's release
  archive and checked against its SHA-512. Auto-update is off and the update host is
  fenced. Before any spending, `isolated-procedure check-agent` must report the version
  the run pinned; an older image fails closed.
- **Model access.** The sandbox gets the run's grant as its Gemini key and
  `/internal/agent-api/{run_id}/gemini` as its Gemini base URL. The relay
  (`agent_api_relay.py`) holds Tin's key. It admits each request against the run's
  contract and budget through the same admission as the Codex relay, then prices the
  usage Google reports on the run's pinned card (`GEMINI_AGENT_CARD`) before the stream's
  last event reaches the harness. Antigravity also calls two helper models of its own,
  Flash-Lite (web search and its tool-call rater) and Gemini 3.8 Flash (titles), and the
  card prices them. Google Search grounding is priced per query. A request Google refuses
  outright (HTTP 429 or 5xx) bills nothing; the harness may resend those bytes up to eight
  times. An unconfirmed request is never bought again.
- **Process.** Codex keeps its model grant in a controller process and runs commands
  through its exec-server as `tin-work`. Antigravity has no such split, so the root helper
  starts the whole harness as `tin-work` (`isolated-procedure agent`). The agent's commands
  can therefore read the run's model grant and its `tin-run` grant. Both expire with the
  run, the model grant buys only the pinned models up to the run's ceiling, and the
  provider keys stay on the switchboard. The trusted controller (`agent_procedure.py`)
  still owns Git, the result, verification and the freeze, and the worker cannot read it.
- **Freedom.** The brief gives the job, the inputs, where things are and the deliverable,
  not a method. The agent may search, read pages through the run's proxy, run commands,
  use subagents and work in `/home/tin-work/scratch`. It gets at least an hour. Changes
  outside the declared outputs are discarded rather than failing the run. The limits are
  runaway guards: the ceiling at the relay, the sandbox timeout and a 20-minute idle stop.
  The agent never publishes, sends or merges; Tin delivers after review.
- **Finishing.** The harness returns `summary` and `message` (and `title` and `body`
  for a pull request) through a JSON schema. When the deliverable is missing or over its
  size, the controller sends the agent back with what to fix, up to three times, instead
  of failing the run.

Codex only, for now: browser and Studio profiles, test identities, and diagram and video
procedures. Host-side output validators (for example `content-draft.v3`) still run after
the sandbox, as for Codex; they are not yet available to the agent inside the run.

## Operating it

- The switchboard needs `GEMINI_API_KEY`. Without it, a procedure that names Antigravity
  is refused at admission and no Gemini route is mounted.
- Rebuild the isolated image (`sandbox/template.py --only tin-lite-codex-isolated`) and
  set `TIN_LITE_E2B_ISOLATED_TEMPLATE` to it. The image keeps Codex unchanged.
- The Gemini project behind the key should have logging off in AI Studio: projects
  created on or after 2026-10-26 log Generate Content calls by default.
- Gemini 3.8 Flash's list price doubles on 2027-01-01. Add a new card before then. A
  pinned card is never changed.
