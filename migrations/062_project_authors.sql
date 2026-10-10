-- Authorship is project metadata, never project membership or publishing permission.
CREATE TABLE project_authors (
    project_id uuid NOT NULL REFERENCES projects(id),
    id uuid NOT NULL,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    member_clerk_user_id text CHECK (member_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    guide_path text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    confirmed_by_clerk_user_id text NOT NULL
        CHECK (confirmed_by_clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, guide_path)
);
CREATE UNIQUE INDEX project_author_member ON project_authors(project_id, member_clerk_user_id)
    WHERE member_clerk_user_id IS NOT NULL;
