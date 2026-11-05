-- AI Visibility: one stored report per project and week, for the AI Visibility page.
--
-- A report says, for each panel question and answer engine, whether the engine's answers named
-- and recommended the project, with each answer's quote, search queries and cited sources. For
-- now an operator imports a week's report with `tin-lite import-ai-visibility`; the page shows
-- once a project has one. `summary` holds the week's totals so the page can draw every week's
-- trend without reading every report. See docs/ai-visibility.md.
CREATE TABLE ai_visibility_weeks (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    week_start date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
    report jsonb NOT NULL,
    summary jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, week_start)
);
