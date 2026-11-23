-- Where a project's RB2B visitors are posted in Slack (rb2b_slack.py). An operator sets it per
-- project for now: the channel, and the project whose Slack connection posts there (for a
-- client, Tin's own workspace and its shared channel with them). No channel, nothing posts.
ALTER TABLE rb2b_hooks
    ADD COLUMN slack_project_id uuid REFERENCES projects(id) ON DELETE SET NULL,
    ADD COLUMN slack_channel_id text CHECK (slack_channel_id ~ '^[A-Z0-9]{1,32}$'),
    ADD COLUMN slack_channel_name text NOT NULL DEFAULT ''
        CHECK (length(slack_channel_name) <= 100);

-- One row per message Tin means to post: a good or strong fit's own message (key
-- `visitor:<run>`), or a day's digest of the rest (key `digest:<local date>`). `sending` is
-- written and committed before Slack is called: a row left there was maybe posted, so it is
-- never sent again and becomes `unknown`.
CREATE TABLE IF NOT EXISTS rb2b_slack_posts (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    post_key text NOT NULL CHECK (length(post_key) BETWEEN 1 AND 100),
    channel_id text NOT NULL CHECK (channel_id ~ '^[A-Z0-9]{1,32}$'),
    status text NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown')),
    message_ts text CHECK (message_ts IS NULL OR message_ts ~ '^[0-9]{1,12}\.[0-9]{1,8}$'),
    error_code text CHECK (error_code IS NULL OR length(error_code) <= 64),
    error_message text CHECK (error_message IS NULL OR length(error_message) <= 300),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    UNIQUE (project_id, post_key)
);
