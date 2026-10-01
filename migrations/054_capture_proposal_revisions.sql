-- A founder's coding agent revises a pending brand or style proposal before the founder decides.
--
-- Emre, 10/1: Decisions keeps Approve and Discard; the agent revises the material instead of a
-- "request changes" button. Each row is one accepted revision of one waiting run: who sent it,
-- through which client, the project revision that holds it and the SHA-256 of every proposal
-- file at that revision. Earlier versions stay readable at their own project revisions.
-- Approval binds the latest row's exact content; a row is never written once the run has a
-- review decision.
CREATE TABLE capture_proposal_revisions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    number integer NOT NULL CHECK (number >= 1),
    request_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    actor_clerk_user_id text NOT NULL CHECK (actor_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    oauth_client_id text,
    client text CHECK (client IS NULL OR client IN ('claude_code', 'codex', 'api')),
    source text NOT NULL CHECK (source IN ('mcp', 'api')),
    base_revision text NOT NULL CHECK (base_revision ~ '^[0-9a-f]{40}$'),
    revision text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
    files jsonb NOT NULL CHECK (
        jsonb_typeof(files) = 'array' AND jsonb_array_length(files) BETWEEN 1 AND 2
    ),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, number),
    UNIQUE (project_id, request_id)
);

CREATE INDEX capture_proposal_revisions_run_idx
    ON capture_proposal_revisions (run_id, number DESC);
