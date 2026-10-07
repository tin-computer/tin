-- website.change: one recorded decision per proposed change to a founder's website.
--
-- A change row has a source, a stable ID, a kind and the site paths it touches. A source
-- proposes rows; the founder approves or declines each row once. The decision records who
-- made it, when, and the exact content it covers (a project revision and a SHA-256), so a
-- later weekly run that proposes the same stable ID never asks again. Approval lives here and
-- in a page run's own review, never in a project file: editing a file cannot publish.
--
-- A page (content_draft) has no row: its review decision in Decisions is its approval.
CREATE TABLE website_changes (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    change_id text NOT NULL CHECK (change_id ~ '^[a-z]{2}_[0-9a-f]{20}$'),
    source text NOT NULL
        CHECK (source IN ('planned_url_change', 'technical_fix', 'blog_index')),
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z_]{1,39}$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    paths jsonb NOT NULL CHECK (
        jsonb_typeof(paths) = 'array' AND jsonb_array_length(paths) BETWEEN 1 AND 20
    ),
    content_revision text CHECK (content_revision IS NULL OR content_revision ~ '^[0-9a-f]{40}$'),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 16000),
    proposed_by_run_id uuid REFERENCES workflow_runs(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'declined')),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    decided_by_clerk_user_id text
        CHECK (decided_by_clerk_user_id IS NULL
               OR decided_by_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    decision_request_id uuid,
    UNIQUE (project_id, change_id),
    UNIQUE (project_id, decision_request_id),
    CHECK (
        (status = 'pending' AND decided_at IS NULL AND decided_by_clerk_user_id IS NULL
            AND decision_request_id IS NULL)
        OR (status <> 'pending' AND decided_at IS NOT NULL
            AND decided_by_clerk_user_id IS NOT NULL AND decision_request_id IS NOT NULL)
    )
);

CREATE INDEX website_changes_pending_idx
    ON website_changes (project_id, proposed_at, change_id)
    WHERE status = 'pending';

-- A decided row never changes again; only project deletion removes it.
CREATE FUNCTION website_changes_decided_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'website change % is already %', OLD.change_id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER website_changes_decided_immutable BEFORE UPDATE ON website_changes
    FOR EACH ROW EXECUTE FUNCTION website_changes_decided_once();
