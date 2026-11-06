-- Films a project's coding agent makes (docs/feature-films.md).
--
-- The founder's coding agent makes a film on its own machine with Tin's film kit and uploads each
-- version through a short-lived link (film_upload_links) that get_film gives it. Tin keeps the
-- version in project Files at films/<slug>.md with its folder, films/<slug>.assets/, and the
-- founder decides on it in Decisions: a directions version is picked from (or sent back for other
-- directions), a film version is approved or sent back with changes. Tin runs nothing to make a
-- film and never waits for one; the agent reads the decision back through get_film.
CREATE TABLE film_versions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 80),
    version integer NOT NULL CHECK (version >= 1),
    kind text NOT NULL CHECK (kind IN ('directions', 'film')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    -- The project revision that holds this version's note and folder.
    commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    -- What Decisions shows: the directions' sketches, or the film's files.
    view jsonb NOT NULL,
    uploaded_by_clerk_user_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- waiting: the founder hasn't decided. picked / others: a directions version's pick, or a
    -- request for other directions. changes / approved: a film version's outcome. superseded:
    -- a newer version came before a decision. discarded: the founder dropped the film.
    status text NOT NULL DEFAULT 'waiting' CHECK (
        status IN ('waiting', 'picked', 'others', 'changes', 'approved', 'superseded', 'discarded')
    ),
    picked integer CHECK (picked IS NULL OR picked BETWEEN 1 AND 8),
    decision_note text NOT NULL DEFAULT '' CHECK (length(decision_note) <= 2000),
    decided_by_clerk_user_id text,
    decided_at timestamptz,
    CHECK ((status = 'picked') = (picked IS NOT NULL)),
    CHECK (kind = 'directions' OR status NOT IN ('picked', 'others')),
    CHECK (kind = 'film' OR status NOT IN ('changes', 'approved')),
    UNIQUE (project_id, slug, version)
);

CREATE INDEX film_versions_waiting ON film_versions (project_id, created_at)
    WHERE status = 'waiting';

-- A link the agent uploads one version through. Only its hash is kept; it expires within the
-- hour and makes at most one version.
CREATE TABLE film_upload_links (
    token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    id uuid NOT NULL UNIQUE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 80),
    created_by_clerk_user_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    version_id uuid REFERENCES film_versions(id) ON DELETE CASCADE
);

CREATE INDEX film_upload_links_project ON film_upload_links (project_id, slug);
