-- Analytics through Agent Traffic's edge service (analytics.agent_traffic). The connection keeps
-- the site Agent Traffic made for the project and its sealed read token; see
-- docs/agent-traffic-connection.md.
ALTER TABLE integration_connections DROP CONSTRAINT integration_connections_provider_key_check;
ALTER TABLE integration_connections ADD CONSTRAINT integration_connections_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x',
                            'network.linkedin', 'social.linkedin', 'analytics.agent_traffic')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_call_receipts DROP CONSTRAINT integration_call_receipts_provider_key_check;
ALTER TABLE integration_call_receipts ADD CONSTRAINT integration_call_receipts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x',
                            'network.linkedin', 'social.linkedin', 'analytics.agent_traffic')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

-- What Tin last read about the project's site, kept apart from the connection's configuration:
-- a run fails when its connection's configuration changes under it, and the site's state moves
-- on its own. `status_claimed_at` spaces calls to the service per project, across processes.
-- `address_run_id` is the Start here run whose address Tin last took, so a newer one that names
-- another host sends the address again.
CREATE TABLE agent_traffic_sites (
    project_id uuid PRIMARY KEY REFERENCES projects(id),
    site_id text NOT NULL CHECK (site_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    address_source text NOT NULL CHECK (address_source IN ('setup', 'entered', 'uncounted_host')),
    address_run_id uuid,
    status jsonb,
    status_checked_at timestamptz,
    status_claimed_at timestamptz,
    status_retry_at timestamptz,
    status_failures integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Tin's stored copy of each report it read for the page and the MCP tool.
CREATE TABLE agent_traffic_reports (
    project_id uuid NOT NULL REFERENCES projects(id),
    site_id text NOT NULL,
    period text NOT NULL CHECK (period IN ('day', 'week')),
    report_date date NOT NULL,
    tz text NOT NULL,
    report jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, site_id, period, report_date, tz)
);

-- Removals Tin still owes the service. A row outlives the project's own data: Tin keeps sending
-- DELETE, waiting longer each time, until the service answers 202, 200 or 404, because the
-- founder's data there has to be deleted. No foreign key, so a purge never takes it.
CREATE TABLE agent_traffic_removals (
    project_ref text PRIMARY KEY,
    project_id uuid NOT NULL,
    site_id text,
    state text NOT NULL CHECK (state IN ('pending', 'removing', 'removed', 'unlinked', 'not_found')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    deleted_by timestamptz,
    finished_at timestamptz
);
CREATE INDEX agent_traffic_removals_due ON agent_traffic_removals (next_attempt_at)
    WHERE finished_at IS NULL;
