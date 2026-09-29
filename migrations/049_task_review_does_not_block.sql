-- A task whose work is finished and only waits for a decision (review, or applying the
-- approved changes) no longer blocks the project's next task. One task may still work, ask a
-- question or be paused at a time. Idempotent.
DROP INDEX IF EXISTS one_active_project_task;

CREATE UNIQUE INDEX IF NOT EXISTS one_working_project_task
ON workflow_runs (project_id)
WHERE executor = 'project.task'
  AND status IN ('pending', 'running', 'needs_input', 'paused')
  AND task_phase IS DISTINCT FROM 'review'
  AND task_phase IS DISTINCT FROM 'applying';
