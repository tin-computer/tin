-- website.change: the change that puts Tin Analytics on a founder's site becomes a change row.
--
-- One 'analytics' row per change: 'install' adds the site's script line, its CSP entries and the
-- request reporter; 'replace' swaps the line for a new site's; 'remove' takes them out again
-- after Stop measuring. Approving the row starts website.change, which writes it in the
-- repository. See docs/agent-traffic-connection.md.
ALTER TABLE website_changes DROP CONSTRAINT website_changes_source_check;
ALTER TABLE website_changes ADD CONSTRAINT website_changes_source_check
    CHECK (source IN ('audit', 'planned', 'blog_index', 'feedback', 'analytics'));

-- The site's reporter token, sealed like a connection credential, so project members can show
-- it again in the reporter step; when a coding agent asked for the reporter, which opens that
-- step; and the site builder the founder pastes the line into, when they picked another than
-- their setup answer named. All of it goes with the site at Stop measuring.
ALTER TABLE agent_traffic_sites
    ADD COLUMN reporter_token_ciphertext bytea,
    ADD COLUMN reporter_token_key_version text,
    ADD COLUMN reporter_token_issued_at timestamptz,
    ADD COLUMN reporter_previous_expires_at timestamptz,
    ADD COLUMN reporter_offered_at timestamptz,
    ADD COLUMN builder text CHECK (builder IS NULL OR builder ~ '^[a-z][a-z0-9_]{0,30}$'),
    ADD CONSTRAINT agent_traffic_sites_reporter_token_check CHECK (
        (reporter_token_ciphertext IS NULL AND reporter_token_key_version IS NULL
            AND reporter_token_issued_at IS NULL)
        OR (reporter_token_ciphertext IS NOT NULL AND reporter_token_key_version IS NOT NULL
            AND reporter_token_issued_at IS NOT NULL)
    );
