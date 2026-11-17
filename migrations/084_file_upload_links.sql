-- Links a project's coding agent uploads files through (file_uploads.py): images, videos and
-- text files it made on the member's machine, which MCP can't carry. prepare_file_upload gives
-- one to a member; it is bound to one folder in the project's Files, acts as that member and
-- expires within the hour. Only a hash of the link is kept.
CREATE TABLE file_upload_links (
    token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    id uuid NOT NULL UNIQUE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    folder text NOT NULL CHECK (length(folder) BETWEEN 1 AND 400),
    created_by_clerk_user_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    files integer NOT NULL DEFAULT 0 CHECK (files >= 0)
);

CREATE INDEX file_upload_links_project ON file_upload_links (project_id, expires_at);
