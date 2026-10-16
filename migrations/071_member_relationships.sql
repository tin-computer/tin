-- Whether each member is connected to a person on LinkedIn. A person can be one teammate's
-- connection and a stranger to another, so this is kept per member, not as one field on the
-- record. A run reads the LinkedIn of the member who started it; a request is that member's.
CREATE TABLE IF NOT EXISTS record_relationships (
    project_id uuid NOT NULL,
    record_id uuid NOT NULL,
    member text NOT NULL CHECK (length(member) BETWEEN 1 AND 200),
    state text NOT NULL CHECK (state IN ('connected', 'pending', 'none')),
    reason text NOT NULL DEFAULT '' CHECK (length(reason) <= 500),
    -- The run that read or changed it.
    set_by text NOT NULL CHECK (length(set_by) BETWEEN 1 AND 200),
    set_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, member),
    FOREIGN KEY (project_id, record_id) REFERENCES records (project_id, id) ON DELETE CASCADE
);

-- What runs recorded until now in the shared field belongs to the member who started each run.
-- The field's values stay where they are; nothing reads them any more.
INSERT INTO record_relationships (project_id, record_id, member, state, reason, set_by, set_at)
SELECT v.project_id, v.record_id, r.started_by_clerk_user_id, v.value_text, v.reason, v.set_by,
       v.set_at
FROM record_values v
JOIN workflow_runs r ON r.id::text = v.set_by
WHERE v.field = 'linkedin_relationship' AND v.set_by_kind = 'run'
  AND v.value_text IN ('connected', 'pending', 'none')
  AND length(r.started_by_clerk_user_id) BETWEEN 1 AND 200
ON CONFLICT (record_id, member) DO NOTHING;
