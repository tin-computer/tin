-- Run summaries and Slack (messaging.slack). A supported workflow's run keeps a short summary of
-- its result; a saved workflow names where to post it, and each run pins that list when it is
-- created. Posting happens after the run succeeds, from run_summary_posts, so a post that fails
-- never fails the run. See docs/run-summaries.md.
ALTER TABLE integration_connections DROP CONSTRAINT integration_connections_provider_key_check;
ALTER TABLE integration_connections ADD CONSTRAINT integration_connections_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x',
                            'network.linkedin', 'social.linkedin', 'analytics.agent_traffic',
                            'messaging.slack')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_call_receipts DROP CONSTRAINT integration_call_receipts_provider_key_check;
ALTER TABLE integration_call_receipts ADD CONSTRAINT integration_call_receipts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x',
                            'network.linkedin', 'social.linkedin', 'analytics.agent_traffic',
                            'messaging.slack')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_auth_attempts DROP CONSTRAINT integration_auth_attempts_provider_key_check;
ALTER TABLE integration_auth_attempts ADD CONSTRAINT integration_auth_attempts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google',
                            'analytics.posthog', 'infra.github_user', 'social.x', 'network.linkedin',
                            'social.linkedin', 'messaging.slack'));

-- [{"provider_key": "messaging.slack", "channel_id": "C0123", "channel_name": "growth"}]
ALTER TABLE project_workflows
    ADD COLUMN summary_destinations jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(summary_destinations) = 'array'
               AND jsonb_array_length(summary_destinations) <= 3);

ALTER TABLE workflow_runs
    ADD COLUMN summary_destinations jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(summary_destinations) = 'array'
               AND jsonb_array_length(summary_destinations) <= 3),
    ADD COLUMN run_summary jsonb CHECK (run_summary IS NULL OR jsonb_typeof(run_summary) = 'object');

-- One row per run and channel. `sending` is written and committed before Slack is called: a row
-- left there by a crash is never sent again, it becomes `unknown`.
CREATE TABLE run_summary_posts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES workflow_runs(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    provider_key text NOT NULL CHECK (provider_key IN ('messaging.slack')),
    channel_id text NOT NULL CHECK (channel_id ~ '^[A-Z0-9]{1,32}$'),
    channel_name text NOT NULL DEFAULT '' CHECK (length(channel_name) <= 100),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    message_ts text CHECK (message_ts IS NULL OR message_ts ~ '^[0-9]{1,12}\.[0-9]{1,8}$'),
    error_code text CHECK (error_code IS NULL OR length(error_code) <= 64),
    error_message text CHECK (error_message IS NULL OR length(error_message) <= 300),
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, provider_key, channel_id)
);

CREATE INDEX run_summary_posts_due ON run_summary_posts (next_attempt_at)
    WHERE status = 'pending';
CREATE INDEX run_summary_posts_project ON run_summary_posts (project_id, created_at DESC);
