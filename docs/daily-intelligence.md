# Daily intelligence

Tin writes a short intelligence brief for a project every morning: what its market said and
did in the last day. Two native workflows do the work, and each runs one agent as a child:

| Workflow | Runs on | Its judge | Output |
|---|---|---|---|
| `intel.world` | the operator's host project, daily 06:00 UTC | `intel.world_judge` | world stories every client reads; `intel/world/<day>/STORIES.md`; a Slack summary in Tin's workspace |
| `intel.daily` | a client project, daily in its time zone | `intel.client_judge` | the client's stories, `intel/reports/<day>/report.md`, and a Slack brief |

The client workflow is documented with its own package. This page covers what both share
and the world feed.

## How a day runs

Code pulls; an agent judges. Fetching is code on the switchboard: paced, bounded reads of
the sources in a config, with no judgment. The judge is an Antigravity procedure on Gemini 4
Neon in the isolated sandbox ([agent harnesses](agent-harnesses.md)). It decides what
happened, records it as stories, and writes a brief. Code renders and posts the brief.

`intel.world`, step by step:

1. **Prepare.** Refuse any project except `TIN_LITE_INTEL_HOST_PROJECT_ID` (a needs-you
   failure, before any spend). Pin the day (the UTC date the run was created), the window and
   the source config. The window starts where the last world day ended if that was within
   36 hours, and 24 hours back otherwise. The config is
   `src/tin_lite/intel_sources/world.json`, published beside the definition and read at the
   run's registry revision.
2. **Fetch.** Three groups (`gnews`, `feeds`, `special`), one activity each, run one at a
   time. Each source's items and status are recorded as it finishes, so a retried group only
   fetches what is missing. A source that fails, comes back empty or runs past its deadline
   is recorded with a reason. A group that fails as a whole, or whose activity never finishes,
   is recorded on each of its sources, as failed with a `group_failed` reason. The day goes on
   either way.
3. **Load.** Give items their final ids and build the day's snapshot. With no items at all
   the run fails here, before the judge: "No items were fetched".
4. **Judge.** Start `intel.world_judge` once, as a billed child on the parent's own registry
   revision (start key `intel:<run>:judge`). If that child dies on a temporary provider
   failure (the model relay's `stream_unconfirmed`, an upstream 5xx or 429, a dropped
   connection, a sandbox server error), Tin starts one more child (`intel:<run>:judge:2`) and
   says so in the run's progress. The first attempt keeps its own receipts and charge, an
   uncertain one stays uncertain, and the stories and brief it wrote stay; the second attempt
   reads them as today's stories. Budget, limit and input refusals are never retried, and
   there is never a third attempt. `intel.daily` does the same. A judge that finishes makes
   the day `ready`. A
   judge that stops with at least 10 stories makes it `partial`, and the run still posts. A
   judge that stops with fewer fails the run with the judge's reason. If no usable brief
   arrived, Tin builds one from the day's high and medium stories, and the summary says Tin
   wrote it.
5. **Unwrap.** Google News links stay Google News links while fetching. Only the links the
   Slack summary shows are unwrapped to the publisher, and the answers are cached for every
   run (`intel_link_cache`). A link that won't unwrap is kept as it is.
6. **Publish.** `intel/world/<day>/STORIES.md` in the host project's Files, rendered by code.
   Then record the world day clients read (`intel_world_days`), `ready` or `partial`. Nothing
   after this step can hide a usable day: a failure later in the run leaves it usable.
7. **Post.** The summary goes to `TIN_LITE_INTEL_WORLD_SLACK_CHANNEL` through the host
   project's `messaging.slack` connection, which is Tin's own Slack app. Its sections run Tech
   and AI, Business, Entertainment and culture, Science and health, then World, which holds at
   most two lines: Tin's clients are tech and consumer companies, so the judge weights the day
   toward tech, business and entertainment and keeps politics to what moves them or dominates
   the news. Each post is recorded
   as `sending` before Slack is called. A post left `sending` becomes `unknown` and is never
   sent again. A refused post is retried for a few hours. Slack never fails the run.
8. **Finish.** Prune, and give the run its result.

Temporal history holds the run ID, the group names, counts and the judge's identifiers.
Items, stories, briefs and messages stay in Postgres and code.storage.

## The judge's sandbox

The judge reads the day with the `intel` command (`sandbox/intel/intel.py`, installed as
`/usr/local/bin/intel`, standard library only):

- `status`, `fronts`, `search`, `count`, `similar`, `browse` and `read` work over a
  read-only SQLite file with full-text search.
- `stories` and `story show` show earlier days' stories and today's.
- `story put`, `story attach` and `brief put` write to Tin.
- `tags` and `brief schema` print the taxonomy and the brief's shape.
- Each call prints at most 9 KB and says what it left out. Antigravity cuts tool output past
  about 10 KB.
- Ids are five characters, from the item's canonical URL, and stay the same across days.
- A source is a configured one: its key and name come from the source config, and its ranks
  are its own order. An item's own `source` is what a reader sees, which for a Google News
  item is the outlet that published it.

Before any spend, the runtime asks the image's root helper for `check-intel`. It needs the
pinned CLI owned by root, SQLite with FTS5, and no `sqlite3` shell, and an older image fails
closed. The switchboard uploads the snapshot. The helper has the CLI load it as the worker,
with no network settings, and installs the result as `/opt/tin-intel/day.db`, `root:root
0444`. The worker can read the day and can't change it.

Writes go through the run's own grant (provider `tin.intel`, capability `intel.write`):

- `POST /internal/run-tools/intel/story`
- `GET /internal/run-tools/intel/stories`
- `GET /internal/run-tools/intel/story/<sid>`
- `PUT /internal/run-tools/intel/brief`
- `GET /internal/run-tools/intel/help`

The grant expires with the run and writes only this run's stories and brief. Scope, day and
project come from the grant's run, never from a request: only `intel.world_judge` on the
host project writes world stories.

Every write is checked, and a refusal comes back as a sentence the agent can act on:

- An item id the run never saw is listed as missing, not refused.
- A tag outside the taxonomy is dropped with a warning.
- Bodies over 64 KB are refused.
- Runaway guards: 3,000 writes, 400 stories per run, 400 items per write.

A brief link must lead to an item of the day or of a recent story, or Tin removes it. Bare
URLs and domains are removed too. The Slack renderer breaks anything Slack could still
auto-link. Whether a story is sensitive, and its default link, always come from the story
rows, never from the brief.

## Stories and tags

A story is one real-world event or development with the items that cover it. Its key stays
the same when the story continues on another day. Stories are kept indefinitely; items for 30
days and snapshots for 14.

- World tags: ai, tech, devtools, enterprise-software, security, outages, business,
  markets, m-and-a, funding, earnings, labor, policy, regulation, legal, elections,
  geopolitics, conflict, disasters, health, healthcare-industry, science, space, climate,
  energy, crypto, consumer, gaming, entertainment, sports, culture, media.
- Client stories may also use: competitor, platform, buyer-voice, product-launch, pricing,
  partnership, hiring, event, research.

`read_intel_stories` (MCP) reads them. `scope` "world" returns the shared world stories, which
any project member can read. `scope` "project" returns the project's own. The result includes
the latest world day's status.

## Billing

- Both parents are spending parents with a $15 maximum, and buy nothing themselves.
- Each judge is session-funded with a $15 ceiling. Observed days cost about $2.
- The world run is billed to the host project's workspace.
- Both parents refuse to start without API-billed Codex execution enabled.
- A scheduled world run needs a standing schedule limit of at least $15 on the host project.

## Operating it

Settings:

- `TIN_LITE_INTEL_HOST_PROJECT_ID`: the host project. Its Slack connection must be Tin's
  own workspace.
- `TIN_LITE_INTEL_WORLD_SLACK_CHANNEL`: the world summary's channel ID. Unset, nothing
  posts.
- `GEMINI_API_KEY` on the switchboard, as for every Antigravity procedure.

The isolated image must carry the intel CLI. A staging image can be built without touching
the shared alias:

```bash
uv run python sandbox/template.py --only tin-lite-codex-isolated --alias tin-lite-codex-isolated-intel
```

Then point `TIN_LITE_E2B_ISOLATED_TEMPLATE` at it. The change is additive: only intel judges
call `check-intel`.

Operator commands:

```bash
tin-lite intel-world enable --created-by <host member's Clerk user id> [--paused]
tin-lite intel-channel <project-id> <channel-id> [<name>]   # or `none`
```

`intel-world enable` saves `intel.world` on the host project with a daily 06:00 UTC schedule,
and does nothing if one is already saved. `intel-channel` sets the channel in Tin's Slack that
a managed client's daily brief posts to.

A failed world run shows in the host project's My system under Needs a look, and in Activity.
Its day is recorded as `failed`, and client briefs say the world stories are unavailable that
day.
