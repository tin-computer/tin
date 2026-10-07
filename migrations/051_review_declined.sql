-- A member can turn down a proposal (a writing style or brand guide) instead of approving it.
-- The run ends as declined, its proposal stays readable in Files, and the active guide is
-- unchanged. A declined review is recorded like an approval: once, with who and when, and a
-- review command that ends the waiting durable run.

ALTER TABLE workflow_runs DROP CONSTRAINT workflow_runs_review_decision_check;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_review_decision_check
    CHECK (review_decision IN ('approved', 'declined'));

ALTER TABLE workflow_runs DROP CONSTRAINT workflow_runs_review_state_check;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_review_state_check CHECK (
    (review_decision IS NULL AND reviewed_at IS NULL)
    OR (
        (review_required OR executor = 'project.task')
        AND review_decision = 'approved'
        AND reviewed_at IS NOT NULL
    )
    OR (review_required AND review_decision = 'declined' AND reviewed_at IS NOT NULL)
);

ALTER TABLE workflow_review_commands DROP CONSTRAINT workflow_review_commands_action_check;
ALTER TABLE workflow_review_commands ADD CONSTRAINT workflow_review_commands_action_check
    CHECK (action IN ('approve', 'revise', 'cancel', 'decline'));
