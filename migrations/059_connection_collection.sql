ALTER TABLE integration_connections DROP CONSTRAINT integration_connections_provider_key_check;
ALTER TABLE integration_connections ADD CONSTRAINT integration_connections_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x', 'network.linkedin')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_call_receipts DROP CONSTRAINT integration_call_receipts_provider_key_check;
ALTER TABLE integration_call_receipts ADD CONSTRAINT integration_call_receipts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x', 'network.linkedin')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_auth_attempts DROP CONSTRAINT integration_auth_attempts_provider_key_check;
ALTER TABLE integration_auth_attempts ADD CONSTRAINT integration_auth_attempts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google',
                            'analytics.posthog', 'infra.github_user', 'social.x', 'network.linkedin'));


CREATE TABLE connection_extension_devices (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    clerk_user_id text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    actor jsonb NOT NULL,
    session_generation bigint NOT NULL DEFAULT 1,
    pairing_hash text UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE connection_collection_jobs (
    run_id uuid PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    connection_id uuid REFERENCES integration_connections(id) ON DELETE SET NULL,
    clerk_user_id text NOT NULL,
    actor jsonb NOT NULL,
    account_lock text NOT NULL,
    inputs jsonb NOT NULL,
    policy jsonb NOT NULL,
    state text NOT NULL DEFAULT 'waiting_browser',
    execution_mode text NOT NULL DEFAULT 'local',
    reason text,
    generation bigint NOT NULL DEFAULT 0,
    lease_hash text,
    lease_device_id uuid REFERENCES connection_extension_devices(id),
    lease_expires_at timestamptz,
    session_generation bigint NOT NULL,
    friend_index integer NOT NULL DEFAULT 0,
    next_page integer NOT NULL DEFAULT 1,
    sources jsonb NOT NULL DEFAULT '{}',
    coverage jsonb NOT NULL DEFAULT '{}',
    cloud_sandbox_id text,
    cloud_deadline timestamptz,
    cloud_template text,
    cloud_transport text,
    cloud_cleanup_confirmed boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz NOT NULL,
    friend_started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (execution_mode IN ('local', 'cloud')),
    CHECK (state IN ('waiting_browser', 'collecting', 'cloud_ready', 'handoff_pending',
                    'paused', 'completed', 'partial', 'failed', 'stopped'))
);
CREATE INDEX connection_collection_pending ON connection_collection_jobs(project_id, state);

CREATE TABLE connection_collection_account_leases (
    account_lock text PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);

CREATE TABLE connection_collection_pages (
    run_id uuid NOT NULL REFERENCES connection_collection_jobs(run_id) ON DELETE CASCADE,
    friend_index integer NOT NULL,
    page integer NOT NULL,
    generation bigint NOT NULL,
    batch_digest text NOT NULL,
    fingerprint text NOT NULL,
    source jsonb NOT NULL,
    records jsonb NOT NULL,
    next_page boolean NOT NULL,
    observed_at timestamptz NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, friend_index, page)
);

-- Setup grants use integration_auth_attempts; credentials remain encrypted in the
-- existing project integration. Device bearers are only stored as hashes.
