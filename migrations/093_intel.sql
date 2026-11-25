-- Daily intelligence (docs/daily-intelligence.md). World rows have project_id NULL (global);
-- client rows are project-scoped and deleted with the project.
CREATE TABLE IF NOT EXISTS intel_fetches (
    run_id uuid PRIMARY KEY REFERENCES workflow_runs(id),
    scope text NOT NULL CHECK (scope IN ('world', 'client')),
    project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
    day date NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    config_sha256 text NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    -- The project commit a client's source configs were read at.
    config_revision text,
    source_status jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(source_status) = 'array'),
    item_count integer NOT NULL DEFAULT 0,
    -- A client run's world day, pinned once by its load step (intel_snapshot.prepare): the
    -- intel_world_days row it used, or {"status": "missing"}. Its snapshot, brief and
    -- sources.json all read this, never the world day as it is later.
    world_day jsonb CHECK (world_day IS NULL OR jsonb_typeof(world_day) = 'object'),
    status text NOT NULL DEFAULT 'fetching'
        CHECK (status IN ('fetching', 'loaded', 'judged', 'partial', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'world') = (project_id IS NULL))
);
CREATE INDEX IF NOT EXISTS intel_fetches_scope ON intel_fetches (project_id, day DESC);

CREATE TABLE IF NOT EXISTS intel_items (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES intel_fetches(run_id) ON DELETE CASCADE,
    -- The intel CLI's id: short_id(canon(url)), lengthened only when two URLs collide.
    item_key text NOT NULL CHECK (item_key ~ '^[a-z2-7]{5,12}$'),
    canon_url text NOT NULL CHECK (length(canon_url) <= 2000),
    source_type text NOT NULL CHECK (source_type IN ('world', 'rss', 'hn', 'reddit', 'x')),
    source text NOT NULL CHECK (length(source) <= 200),
    source_id text NOT NULL CHECK (length(source_id) <= 100),
    source_url text CHECK (length(source_url) <= 2000),
    url text NOT NULL CHECK (length(url) <= 2000),
    discussion_url text CHECK (length(discussion_url) <= 2000),
    publisher_site text CHECK (length(publisher_site) <= 300),
    title text CHECK (length(title) <= 500),
    author text CHECK (length(author) <= 200),
    published_at timestamptz,
    rank integer,
    engagement jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(engagement) = 'object'),
    text text NOT NULL DEFAULT '' CHECK (length(text) <= 8000),
    fetched_at timestamptz NOT NULL,
    UNIQUE (run_id, canon_url)
);
CREATE INDEX IF NOT EXISTS intel_items_key ON intel_items (run_id, item_key);

CREATE TABLE IF NOT EXISTS intel_snapshots (
    run_id uuid PRIMARY KEY REFERENCES intel_fetches(run_id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format = 'intel-snapshot/1'),
    sha256 text NOT NULL,
    bytes integer NOT NULL CHECK (bytes BETWEEN 1 AND 33554432),
    body bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS intel_stories (
    id uuid PRIMARY KEY,
    -- NULL is the world feed.
    project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
    key text NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9-]{2,60}$'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 2000),
    relevance text CHECK (relevance IN ('high', 'medium', 'low')),
    tags text[] NOT NULL DEFAULT '{}' CHECK (cardinality(tags) <= 12),
    sensitive boolean NOT NULL DEFAULT false,
    continues_id uuid REFERENCES intel_stories(id),
    first_day date NOT NULL,
    last_day date NOT NULL,
    created_run_id uuid NOT NULL,
    updated_run_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS intel_stories_world_key
    ON intel_stories (key) WHERE project_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS intel_stories_project_key
    ON intel_stories (project_id, key) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS intel_stories_recent ON intel_stories (project_id, last_day DESC);

-- What a story said on each day it was touched.
CREATE TABLE IF NOT EXISTS intel_story_days (
    story_id uuid NOT NULL REFERENCES intel_stories(id) ON DELETE CASCADE,
    day date NOT NULL,
    run_id uuid NOT NULL,
    title text NOT NULL,
    summary text NOT NULL,
    relevance text,
    sensitive boolean NOT NULL,
    PRIMARY KEY (story_id, day)
);

-- Denormalized, so a story outlives the retention of the items it was built from.
CREATE TABLE IF NOT EXISTS intel_story_items (
    story_id uuid NOT NULL REFERENCES intel_stories(id) ON DELETE CASCADE,
    day date NOT NULL,
    run_id uuid NOT NULL,
    item_key text NOT NULL,
    url text NOT NULL CHECK (length(url) <= 2000),
    title text,
    source text NOT NULL,
    rank integer,
    -- The order the judge attached them in: strongest outlets first.
    attached_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (story_id, url)
);

CREATE TABLE IF NOT EXISTS intel_briefs (
    -- The judge child's run.
    run_id uuid PRIMARY KEY REFERENCES workflow_runs(id),
    parent_run_id uuid NOT NULL REFERENCES workflow_runs(id),
    project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
    day date NOT NULL,
    schema text NOT NULL CHECK (schema IN ('intel-world-brief/1', 'intel-brief/1')),
    brief jsonb NOT NULL CHECK (jsonb_typeof(brief) = 'object'),
    source text NOT NULL CHECK (source IN ('judge', 'fallback')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS intel_briefs_parent ON intel_briefs (parent_run_id);

CREATE TABLE IF NOT EXISTS intel_world_days (
    day date PRIMARY KEY,
    run_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('ready', 'partial', 'failed')),
    stories integer NOT NULL DEFAULT 0,
    items integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The channel an operator set for a managed client's brief (tin-lite intel-channel).
CREATE TABLE IF NOT EXISTS intel_projects (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    slack_channel_id text CHECK (slack_channel_id ~ '^[A-Z0-9]{1,32}$'),
    slack_channel_name text NOT NULL DEFAULT '' CHECK (length(slack_channel_name) <= 100),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Recorded as 'sending', and committed, before Slack is called.
CREATE TABLE IF NOT EXISTS intel_slack_posts (
    id uuid PRIMARY KEY,
    project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
    post_key text NOT NULL CHECK (length(post_key) BETWEEN 1 AND 100),
    channel_id text NOT NULL CHECK (channel_id ~ '^[A-Z0-9]{1,32}$'),
    status text NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown')),
    message_ts text CHECK (message_ts IS NULL OR message_ts ~ '^[0-9]{1,12}\.[0-9]{1,8}$'),
    error_code text CHECK (length(error_code) <= 64),
    error_message text CHECK (length(error_message) <= 300),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    UNIQUE (post_key, channel_id)
);

-- A Google News article id and the publisher URL it leads to, shared by every run.
CREATE TABLE IF NOT EXISTS intel_link_cache (
    gid text PRIMARY KEY CHECK (length(gid) <= 600),
    url text NOT NULL CHECK (url ~ '^https?://' AND length(url) <= 2000),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The judges' run-bound grant for their intel writes (run_tools.py /intel routes).
ALTER TABLE run_tool_grants DROP CONSTRAINT run_tool_grants_provider_key_check;
ALTER TABLE run_tool_grants ADD CONSTRAINT run_tool_grants_provider_key_check
    CHECK (provider_key IN ('workspace.google', 'tin.studio', 'tin.services', 'tin.intel'));
ALTER TABLE run_tool_grants DROP CONSTRAINT run_tool_grants_connection_matches_provider_check;
ALTER TABLE run_tool_grants ADD CONSTRAINT run_tool_grants_connection_matches_provider_check
    CHECK ((provider_key IN ('tin.studio', 'tin.services', 'tin.intel')) = (connection_id IS NULL));
