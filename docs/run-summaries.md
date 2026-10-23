# Run summaries and Slack

A run summary is the short, self-contained read of a run's result. For example, the top of a
visitor journal: a funnel table, a few named visitors and a few observations. Tin keeps it on
the run. A saved workflow can post each run's summary to Slack channels the founder picks, after
the run succeeds.

The code lives in `src/tin_lite/run_summaries.py` (the shape and what supports it),
`src/tin_lite/slack_connection.py` (the `messaging.slack` connection) and
`src/tin_lite/summary_posts.py` (where to post, and posting). Migration
`079_run_summaries_slack.sql` adds the columns and the `run_summary_posts` table.

## The summary

A summary is format-neutral data, not Markdown or a provider's message format:

| Field | Bounds |
|---|---|
| `headline` | one line, at most 300 characters; required |
| `table` | optional; `columns` (2 to 6) and `rows` (at most 10), every cell text of at most 60 characters |
| `sections` | at most 4, each a `heading` and at most 8 `items`; an item is `text` and an optional `https` `url` with a short `label` |

`run_summaries.parse` keeps what fits and drops a summary without a headline. A summary that
breaks the shape is dropped, never grounds to fail a run that already published its result.

## Which workflows write one

- **A procedure** whose output validator has a reader in `run_summaries.SOURCES`. Today that is
  `visitor-journal.v1` (`product.visitor_journeys`): the brief's table, visitors with replay
  links, and observations, under the run's one-line summary. To add a procedure, add a reader
  for its validator.
- **A code workflow** that declares `"summary": true` in `code.output` and returns `summary`
  beside `path` and `content`:

  ```python
  return {
      "path": "reports/weekly/ORDERS.md",
      "content": report,
      "summary": {
          "headline": "12 orders this week, 3 over $500.",
          "table": {"columns": ["", "Orders", "Revenue"], "rows": [["This week", "12", "$4,210"]]},
          "sections": [{"heading": "Largest", "items": [{"text": "Acme: $1,200"}]}],
      },
  }
  ```

  Without the declaration the result must still be exactly `path` and `content`.

The summary is saved with the publication receipt and stored on `workflow_runs.run_summary`
when the result is published. `get_run` over MCP returns it as `run_summary`, with
`summary_posts`. The REST run view returns `run_summary`.

## Where it is posted

A saved workflow has `summary_destinations`: up to three Slack channels, each with the
channel's ID and name. Each run copies the list when it is created (`create_run`), like its
inputs, so a change affects later runs only.

- Dashboard: My system → open the saved workflow → **Post the summary to Slack**. The control
  shows only for workflows that write a summary, on a deployment with Slack configured.
- REST: `PUT /api/projects/{id}/workflows/{project_workflow_id}/summary-destinations` with
  `{"slack_channel_ids": ["C0123"]}`, or `[]` to stop.
- MCP: `list_slack_channels`, then `set_summary_destinations`. The saved workflow's view says
  `summary_supported` and lists `summary_destinations`.

Saving checks the saved workflow's pinned definition writes a summary and that each channel is a
channel Tin's app can post to in the connected workspace. Clearing never needs Slack.

## Posting

Posting never changes a run that already succeeded:

1. The transaction that marks a run `succeeded` (`complete_procedure_projection`) also inserts
   one `pending` row in `run_summary_posts` per pinned destination when the run has a summary.
2. A switchboard loop (`summary_posts_loop`, every 10 seconds) claims due rows one at a time.
   It marks each `sending` and commits before calling Slack, so a deploy can interrupt only the
   post in flight.
3. It settles each row:
   - `sent`, with Slack's message `ts`;
   - `failed` when Slack refused with an answer that means nothing was posted: the channel is
     gone, archived or closed to apps, the app was removed, or the token was revoked;
   - `pending` again when nothing was posted and a later try can work, at most 5 attempts:
     a rate limit (Slack posts nothing on a 429), or Slack could not be reached at all;
   - `unknown` when the outcome is not certain: Slack's answer was lost, or Slack answered with
     an error Tin does not know, `internal_error` and `fatal_error` among them. The message may
     be in the channel, so it is never sent again.
4. A row left `sending` for 10 minutes, because Tin stopped mid-send, becomes `unknown`.

Activity records `run_summary_posted` or `run_summary_not_posted` with the channel and the
reason. A revoked or invalid token marks the Slack connection *needs attention*; Reconnect
restores it. A refusal for one channel leaves the connection alone.

Deleting a project deletes its posts and clears the summaries and destinations on the runs it
keeps for billing.

The message is a header (saved workflow name and project), the headline, the table as aligned
monospace text, each section as a bulleted list with its links, and a link to the full result in
Tin. Text is escaped for Slack, so a summary can never mention `@channel`, and link previews are
off. Each block keeps whole rows and items within Slack's 3,000 characters.

Summaries can name people, such as a visitor journal's named visitors with their email. The
founder chooses the channel; pick one whose members may see that.

## Slack connection

`messaging.slack` uses Slack OAuth v2 with a bot token and the scopes `channels:read`,
`groups:read`, `chat:write` and `chat:write.public`. Tin lists up to 2,000 channels and posts; it
never reads messages. The token is stored encrypted and is revoked (`auth.revoke`) on disconnect.

- **Public channels**: Tin can post to any of them; the app does not need to join.
- **Private channels**: Slack shows a bot only the private channels it is in, so someone in the
  channel adds Tin's app first (`/invite @<app name>`, or the channel's *Integrations* tab). The
  channel then appears in the list, marked private. If the app is later removed, posts to that
  channel are refused (`not_in_channel` or `channel_not_found`) and Activity says so.

Operator setup:

1. Create the Slack app from [`infra/slack-app-manifest.json`](../infra/slack-app-manifest.json):
   api.slack.com/apps → *Create New App* → *From a manifest* → pick the workspace → paste the
   file (JSON). It sets the name, the bot user, the four bot scopes above, token rotation off,
   and the redirect URLs for app.tin.computer and app.staging.tin.computer. A self-hosted
   deployment replaces them with `{TIN_LITE_PUBLIC_URL}/integrations/callback/slack`. A test
   keeps the manifest's scopes and callback path equal to the code's.

   To install in workspaces other than the app's own, turn on *Manage Distribution*; a manifest
   cannot. Token rotation cannot be turned off again once on, so never enable it.
2. Set `TIN_LITE_SLACK_OAUTH_CLIENT_ID` and `TIN_LITE_SLACK_OAUTH_CLIENT_SECRET` (both or
   neither). They need `TIN_LITE_INTEGRATION_CREDENTIAL_KEY`.
3. Apply migration 079 before deploying.

Without the two settings the Slack card shows as not configured, and the dashboard hides the
control.
