-- A project setting: the founder's extra protected pages for website.change.
--
-- A change to a protected page always opens a pull request for the founder to merge, even when
-- approved. /sign-in, /sign-up and /auth-complete are always protected; this table holds the
-- pages the founder adds on top (pages another app shares, for example).
--
-- Each save appends one revision with who saved it, when, and the request that saved it, so
-- the newest revision is the setting and the older ones are its history. A revision never
-- changes after it is written; only project deletion removes them.
CREATE TABLE project_protected_paths (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    paths jsonb NOT NULL CHECK (
        jsonb_typeof(paths) = 'array' AND jsonb_array_length(paths) <= 20
    ),
    changed_by_clerk_user_id text NOT NULL
        CHECK (changed_by_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    changed_at timestamptz NOT NULL DEFAULT now(),
    request_id uuid NOT NULL,
    PRIMARY KEY (project_id, revision),
    UNIQUE (project_id, request_id)
);

CREATE FUNCTION project_protected_paths_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'protected path revision % of project % is history', OLD.revision,
        OLD.project_id;
END;
$$;
CREATE TRIGGER project_protected_paths_history BEFORE UPDATE ON project_protected_paths
    FOR EACH ROW EXECUTE FUNCTION project_protected_paths_append_only();
