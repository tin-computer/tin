-- model_call_logs: each Neon call's full prompt and answer, for explaining a verdict or a
-- rating and improving the prompts later (model_logs.py). One row per call: what was sent
-- (instructions, messages, schema, settings), what came back (text, parsed answer, the searches
-- it ran and the pages it rested on) or why nothing did. Prompts hold people's details, so rows
-- are kept KEEP_DAYS (90) and go with their project.
CREATE TABLE IF NOT EXISTS model_call_logs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    run_id uuid NOT NULL,
    step text NOT NULL CHECK (length(step) BETWEEN 1 AND 200),
    route text NOT NULL,
    request jsonb NOT NULL,
    response jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS model_call_logs_run ON model_call_logs (run_id, created_at);
CREATE INDEX IF NOT EXISTS model_call_logs_created ON model_call_logs (created_at);
