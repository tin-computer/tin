-- RB2B posts each visitor it identifies on a project's site to that project's own webhook URL
-- (rb2b.py). RB2B sends no signature or account ID, so the URL's token is both the address and
-- the credential: Tin keeps its hash to find the project and the token itself sealed, to show
-- the URL again. One hook per project; setting it up again replaces the token.
CREATE TABLE IF NOT EXISTS rb2b_hooks (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    token_ciphertext bytea NOT NULL,
    -- The member whose runs rate the visitors: they set it up.
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_received_at timestamptz,
    last_test_at timestamptz
);

-- Every payload RB2B posted, kept as sent. A person starts a run (run_id); a company-only visit
-- waits for the workflow that finds someone there; RB2B's test event only marks the hook.
CREATE TABLE IF NOT EXISTS rb2b_visits (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    dedupe_key text NOT NULL CHECK (length(dedupe_key) = 64),
    kind text NOT NULL CHECK (kind IN ('person', 'company', 'test')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    received_at timestamptz NOT NULL DEFAULT now(),
    run_id uuid REFERENCES workflow_runs(id) ON DELETE SET NULL,
    -- Why the run couldn't start, Tin's own words; the next visit to the project tries again.
    start_error text CHECK (length(start_error) <= 500),
    UNIQUE (project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS rb2b_visits_waiting
    ON rb2b_visits (project_id, received_at) WHERE kind = 'person' AND run_id IS NULL;
