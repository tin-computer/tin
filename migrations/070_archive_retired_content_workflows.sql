-- content.answer_page, content.public_article, organic.technical_fix and content.deliver left
-- the codebase: content.generate writes every page and website.change puts pages and audit
-- fixes on the site. Their registry rows stay so past runs keep their pinned definitions, but
-- archived rows no longer list, resolve, or start. Saved configurations of them are archived
-- too; an operator deletes their Temporal schedules before deploying.
UPDATE workflows
SET status = 'archived', updated_at = now()
WHERE project_id IS NULL
  AND id IN (
    '00000000-0000-4000-8000-000000000005',
    '00000000-0000-4000-8000-000000000009',
    '00000000-0000-4000-8000-000000000028',
    '00000000-0000-4000-8000-000000000036'
  )
  AND status <> 'archived';

UPDATE project_workflows
SET status = 'archived', next_run_at = NULL, updated_at = now()
WHERE workflow_id IN (
    '00000000-0000-4000-8000-000000000005',
    '00000000-0000-4000-8000-000000000009',
    '00000000-0000-4000-8000-000000000028',
    '00000000-0000-4000-8000-000000000036'
  )
  AND status <> 'archived';
