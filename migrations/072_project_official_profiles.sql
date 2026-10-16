-- A project setting: the business's official profiles and the company that publishes the site.
--
-- The organic audit lists candidates (profile pages the site links to or that link to it, and a
-- site that links to it from its footer by name); a project member confirms the ones that are
-- really the business's. website.change writes only these into the homepage's structured data
-- (sameAs, and publisher or parentOrganization), never a candidate on its own.
--
-- Each save appends one revision with who saved it, when, and the request that saved it, so
-- the newest revision is the setting and the older ones are its history. A revision never
-- changes after it is written; only project deletion removes them.
CREATE TABLE project_official_profiles (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    profiles jsonb NOT NULL CHECK (
        jsonb_typeof(profiles) = 'array' AND jsonb_array_length(profiles) <= 20
    ),
    parent_organization jsonb CHECK (
        parent_organization IS NULL OR jsonb_typeof(parent_organization) = 'object'
    ),
    changed_by_clerk_user_id text NOT NULL
        CHECK (changed_by_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    changed_at timestamptz NOT NULL DEFAULT now(),
    request_id uuid NOT NULL,
    PRIMARY KEY (project_id, revision),
    UNIQUE (project_id, request_id)
);

CREATE FUNCTION project_official_profiles_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'official profile revision % of project % is history', OLD.revision,
        OLD.project_id;
END;
$$;
CREATE TRIGGER project_official_profiles_history BEFORE UPDATE ON project_official_profiles
    FOR EACH ROW EXECUTE FUNCTION project_official_profiles_append_only();
