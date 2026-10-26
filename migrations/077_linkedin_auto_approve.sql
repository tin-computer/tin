-- A member can let Tin approve the people their own LinkedIn reads propose, once sending is on
-- (`auto_approve`); see docs/linkedin-requests.md. The sending run Tin starts for such a batch
-- records `auto_approve` as what started it.
ALTER TABLE linkedin_sending ADD COLUMN auto_approve boolean NOT NULL DEFAULT false;

ALTER TABLE workflow_runs
    DROP CONSTRAINT workflow_runs_trigger_source_check,
    ADD CONSTRAINT workflow_runs_trigger_source_check
        CHECK (trigger_source IN ('manual', 'chat', 'mcp', 'schedule', 'api', 'auto_approve'));
