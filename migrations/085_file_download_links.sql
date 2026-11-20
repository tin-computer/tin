-- Short-lived links to one project file at one revision (image_library.py). get_image gives an
-- agent one per file, because MCP carries no file bytes and an agent holds no browser session.
-- A link names one path at one commit, acts as the member who asked for it and expires within
-- 15 minutes. Only a hash of the link is kept.
CREATE TABLE file_download_links (
    token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path text NOT NULL CHECK (length(path) BETWEEN 1 AND 512),
    revision text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
    created_by_clerk_user_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX file_download_links_expiry ON file_download_links (expires_at);
