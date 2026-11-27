# Daily intelligence

Tin writes a short intelligence brief for a project every morning: what its market said and
did in the last day. Two native workflows do the work, and each runs one agent as a child:

| Workflow | Runs on | Its judge | Output |
|---|---|---|---|
| `intel.world` | the operator's host project, daily 06:00 UTC | `intel.world_judge` | world stories every client reads; `intel/world/<day>/STORIES.md`; a Slack summary in Tin's workspace |
| `intel.daily` | a client project, daily in its time zone | `intel.client_judge` | the client's stories, `intel/reports/<day>/report.md`, and a Slack brief |

This page covers what both share and the world feed, then the client brief in
[A client's daily brief](#a-clients-daily-brief-inteldaily).

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

- Both parents are spending parents with a $15 maximum. The world parent buys nothing
  itself; the client parent buys only its X List pages (below).
- Each judge is session-funded with a $15 ceiling. Observed days cost about $2 for the world
  and $2.30 to $3.80 for a client.
- The world run is billed to the host project's workspace (Tin Computer in production). It is
  admitted like any paid run: the workspace needs run billing on, and credits or invoiced
  funding for the estimate, and the host project's limits must cover $15 a run. A scheduled
  world run needs a standing schedule limit of at least $15.
- A client's X List pages are billed to the client's run at X's pay-per-use price per post
  returned (pinned in the run's terms as `x_read_pricing`, $0.005 a post today), through the
  same receipt and charge path as other priced provider calls. Each page reserves its ceiling
  (100 posts) before X is asked and is charged for what X returned. A page whose answer never
  arrived is never bought again and never counted as zero: it stays pending until the run
  settles, like any unconfirmed paid call.
- Both parents refuse to start without API-billed Codex execution enabled.

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

## A client's daily brief (`intel.daily`)

`intel.daily` reads one client's market every morning, has an analyst agent judge it, and
publishes a detailed report and a short executive brief. It is a saved project workflow,
scheduled daily at 09:00 in the project's timezone and billed to the project like any other
workflow.

### What the project needs

- `company.md` at the root of the project files (`context/company.md` also works): who the
  client is, its buyers, competitors and team, and in section 7 the claims it may and may not
  make. The analyst follows section 7 in everything it writes. Without it the run is refused
  before anything is fetched or charged.
- At least one source config. Without either, the run is refused the same way.
  - `intel/sources/rss.json`: `{"sources": [...], "exclude_outlets": [...],
    "exclude_domains": [...], "enrich_text_below_chars": 280}`. Each source has an `id`, a
    `name` and a `method`:
    - `rss`: a feed `url`;
    - `google_news`: a search `query`;
    - `html_links`: a listing page `url`, and `link_paths` naming the article links;
    - `sitemap`: a sitemap `url`, and `url_paths` naming the pages to read;
    - `crossref`: a journal `issn`.
  - `intel/sources/hn-reddit.json`: `{"hn": {"queries": [{"q", "tags"}], "extra_params",
    "quote_multiword", "thread_comments"}, "reddit": {"subreddits": [{"name", "all_posts",
    "post_queries", "comment_queries"}], "thread_comments"}}`. Tin uses its own endpoints
    (Algolia for HN, Arctic Shift for Reddit), whatever the file says.
- Optional: an X List on Tin's own X account, pinned for the client by an operator
  (`tin-lite intel-x-list`). It is read through the X connection on the intel host project,
  never through an X connection of the client's.

`link_paths` and `url_paths` are path globs, not regular expressions. Configs are project files
any member can edit, and Tin's worker never runs a pattern from one. A glob is a host (with or
without `www.`) and path segments:

- `*` stands for exactly one segment;
- `**` stands for one or more, and only as the last part;
- everything else is literal.

A trailing slash, the query and the fragment are ignored. For example,
`vendor.example/blog/*` and `vendor.example/news/**`. A source can have up to 20 globs of up to
200 characters. A source that still uses `link_pattern` or `url_pattern` is skipped with that
reason.

A source that fails its checks is skipped with a reason in the run's source status, and the rest
of the day goes on.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `post_brief` | `true` | Post the brief in Slack when a channel is set |
| `x_keep_retweets` | `false` | Keep plain retweets (dropped by default: mostly truncated copies) |
| `lookback_hours` | 24 | 12 to 72; used when there is no recent run to continue from |

### The X List

Every client's List lives on one X connection: the intel host project's
(`TIN_LITE_INTEL_HOST_PROJECT_ID`, Tin Computer in production). Connecting one X account to
several Tin projects keeps only the newest connection, so the client's project needs no X of its
own, and one it has is ignored.

- **The List is the operator's pin, not an input.** `tin-lite intel-x-list <project> <list_id>`
  stores it on the client's `intel_projects` row; a List can be pinned for one client only.
  Members of a client project can't choose a List, so they can't point Tin's X account at
  another client's. Saved workflows from the first definition may still carry an `x_list_id`
  input: a run reads X only when it equals the pin.
- **Ownership is checked.** Before any page, one receipted lookup asks X who owns the List; Tin
  reads it only when Tin's own account does. The read touches nothing on the account but that
  List.
- **Who reads it.** The List is read as whoever connected X on the host project, through that
  connection's own member check; who saved or started the client's workflow doesn't matter.
- **Receipts.** The ownership lookup and every page have receipts keyed by the client's run
  (`intel:<run>:x:<list>:owner`, `intel:<run>:x:<list>:<page>`), and so do its items. The X call
  records carry the client's run and the host's connection. Each page's receipt is its usage
  receipt, and the client's run is charged from it (see Billing below).
- **When X isn't read**, the reason is in `sources.json` and, except for a client with no List,
  in a line of the brief:

  | Reason | When |
  |---|---|
  | `x_no_host` | No intel host project is set |
  | `x_not_enrolled` | The client has no operator setup (`intel_projects` row) |
  | `x_no_list` | No List is pinned for the client |
  | `x_list_not_enrolled` | The run's old `x_list_id` input names a different List |
  | `x_not_connected`, `x_reauthorize` | X isn't connected on the host, or needs reconnecting |
  | `x_list_unavailable` | X doesn't find the List, or won't serve it to Tin's account |
  | `x_list_not_owned` | Tin's account doesn't own the List |
  | `x_member` | X on the host was reconnected by someone else mid-read |
  | `x_spending` | The run's budget or the project's limits had no room for another page |

### How a run goes

1. **Prepare** pins these, so a later edit changes only future runs:
   - the client's day, which is the local date in the project's timezone;
   - the window, which ends at the run's start. It continues from the last successful run with
     an hour of overlap, or else looks back `lookback_hours`;
   - the project revision the configs were read at;
   - the Slack destination;
   - whether X is read: the host project and the List pinned for the client.
2. **Fetch** reads in groups, one after another: feeds, Google News, pages (html_links, sitemap,
   crossref), HN, Reddit and X.
   - Each source has a deadline, longer for a subreddit.
   - A retried group skips the sources it already recorded.
   - Google News links stay Google News links until after the judge.
3. **Load** builds the day the analyst reads:
   - it records any source a fetch group never finished as failed, with the group's reason;
   - it adds the stories of the newest usable world day (`ready` or `partial`), and pins that
     day on the run, so the snapshot, the brief and `sources.json` all name the same one;
   - a client's own item for the same URL as a world story keeps its row, with a line naming
     the world story;
   - it gives every item its id;
   - it stores one snapshot.

   If the client's own sources gave nothing, the run fails here, before the analyst is paid for.
4. **Judge**: `intel.client_judge` (Antigravity on Gemini 4 Neon, high effort, in the isolated
   sandbox) runs as the run's child. It reads the day with the `intel` command and does three
   things:
   - it records the day's stories;
   - it writes the eight-section `report.md`;
   - it sends the brief as `intel-brief/1` JSON, which Tin checks. Links must lead to the day's
     items, and the brief carries no notes on its own evidence: a single-source item says
     who reported it, and what needs checking stays in the report's Source notes.
5. **Publish** unwraps the Google News links that the report and brief use. Then one commit
   adds `intel/reports/<day>/report.md`, `brief.md` and `sources.json` (the status of every
   source).
6. **Post** sends the brief through Tin's own Slack app (the host project's
   `messaging.slack` connection) to the channel an operator set for the client.
   - The message has the day's headline, three to five developments with their outlets, and
     buttons to the report and the source status.
   - Suggestions stay in the report.
   - The post is recorded before Slack is called and is never sent twice.

### When part of the day is missing

| Situation | What happens |
|---|---|
| A source fails, is empty or is skipped | Recorded in `sources.json`; the run goes on |
| A whole fetch group fails or never finishes | Each of its sources is recorded as failed; the run goes on |
| X not connected on the host project, reconnect needed, List not found, not readable or not Tin's | X skipped with the reason; the brief says so |
| No host project, no operator setup, no pinned List, or an input naming another List | X skipped with the reason |
| An X page's purchase unconfirmed | Never bought again; X stops there with what it read |
| No usable world day at load | The run goes on; the brief says world stories are unavailable |
| The analyst sends no usable brief | Tin writes one from the day's high and medium stories, and the brief says so |
| The analyst fails | The run fails with the analyst's own reason; its stories stay |
| No Slack channel set, or Slack refuses | Nothing is posted; the run still succeeds |

### Billing

Billing is as described [above](#billing): $2.30 to $3.80 a day for the analyst. The client
also pays for its X List pages, read through Tin's X: each page reserves 100 posts' worth
($0.50) before X is asked and is charged $0.005 for each post X returned, on the client's run.
A page X refused is charged nothing; a page whose answer was lost stays pending until the run
settles. Tin's X ownership lookup, one per run, isn't charged.

### Operator setup

1. Set `TIN_LITE_INTEL_HOST_PROJECT_ID` to the project whose Slack connection is Tin's own app.
2. Connect X on the host project with Tin's own account (`@tincomputer` in production). Keep
   that account connected to the host only: connecting it to another project drops the host's.
3. Set the client's channel with `tin-lite intel-channel <project_id> <channel_id> "<name>"`.
   Use `none` as the channel ID to stop posting.
4. Create the client's List on Tin's X account and pin it with
   `tin-lite intel-x-list <project_id> <list_id>`. Use `none` to stop reading X.
5. Commit `company.md` and the source configs to the client's project, and save `intel.daily`
   on it.
