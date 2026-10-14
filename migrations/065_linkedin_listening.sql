-- LinkedIn listening: Tin's own readings of each post's results, and each connected account's
-- follower count per day. Zernio keeps no hourly history, so these rows are the record;
-- social/linkedin/results.json and RESULTS.md are rewritten from them.
CREATE TABLE linkedin_posts (
    project_id uuid NOT NULL REFERENCES projects(id),
    urn text NOT NULL CHECK (urn ~ '^urn:li:(share|ugcPost|activity):[0-9]{1,30}$'),
    account_id text NOT NULL CHECK (account_id ~ '^[0-9a-f]{24}$'),
    account_urn text NOT NULL,
    account_kind text NOT NULL CHECK (account_kind IN ('personal', 'organization')),
    account_name text NOT NULL,
    url text,
    published_at timestamptz NOT NULL,
    -- A post Tin published keeps its publish run and the draft it came from.
    publish_run_id uuid REFERENCES workflow_runs(id),
    draft_path text,
    draft_post_id text,
    excerpt text NOT NULL DEFAULT '',
    removed_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, urn)
);
CREATE INDEX linkedin_posts_recent ON linkedin_posts (project_id, published_at DESC);

CREATE TABLE linkedin_post_readings (
    project_id uuid NOT NULL,
    urn text NOT NULL,
    -- 1h, 4h, 24h or 72h after publishing, or d<N>: the daily reading N days after.
    checkpoint text NOT NULL CHECK (checkpoint ~ '^(1h|4h|24h|72h|d[0-9]{1,2})$'),
    taken_at timestamptz NOT NULL DEFAULT now(),
    metrics jsonb NOT NULL,
    PRIMARY KEY (project_id, urn, checkpoint),
    FOREIGN KEY (project_id, urn) REFERENCES linkedin_posts (project_id, urn) ON DELETE CASCADE
);

CREATE TABLE linkedin_follower_counts (
    project_id uuid NOT NULL REFERENCES projects(id),
    account_urn text NOT NULL,
    day date NOT NULL,
    followers integer NOT NULL CHECK (followers >= 0),
    PRIMARY KEY (project_id, account_urn, day)
);
