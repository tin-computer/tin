-- What kind of failure stopped a run, kept apart from its founder-safe error_message.
-- error_detail is the raw, scrubbed cause for operators; it is never returned by the API or MCP.
ALTER TABLE workflow_runs
    ADD COLUMN error_kind text
        CHECK (error_kind IN ('needs_you', 'transient', 'explained', 'internal')),
    ADD COLUMN error_action text CHECK (length(error_action) <= 40),
    ADD COLUMN error_detail text CHECK (length(error_detail) <= 2000);
