-- Someone who connects with a member after Tin first read that member's LinkedIn connections
-- gets a `new_connection` event on their record, beside the `connected` one: a new connection
-- did something Tin saw, so People lists them with everyone who engaged rather than under
-- Connections. Only the allowed kinds change.
ALTER TABLE record_events DROP CONSTRAINT IF EXISTS record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'viewed', 'visited', 'invited', 'accepted', 'withdrawn',
                    'connected', 'starred', 'new_connection'));

-- Each member's connections list as Tin has read it in a project:
-- - baseline_at: when Tin first read it. A connection LinkedIn dates after this is new.
-- - read_to_end_at: when a run last read the list to its end (or to the most Tin lists). From
--   then on a run reads only until it reaches connections it already has.
CREATE TABLE IF NOT EXISTS linkedin_connection_lists (
    project_id uuid NOT NULL REFERENCES projects(id),
    member text NOT NULL,
    baseline_at timestamptz NOT NULL,
    read_to_end_at timestamptz,
    PRIMARY KEY (project_id, member)
);

-- Members whose list Tin has already read: their baseline is the first connection Tin recorded
-- for them. The next run reads their list to the end once more before it stops early.
INSERT INTO linkedin_connection_lists (project_id, member, baseline_at)
SELECT e.project_id, w.started_by_clerk_user_id, min(e.recorded_at)
FROM record_events e
JOIN workflow_runs w ON w.id = e.run_id
WHERE e.kind = 'connected' AND w.started_by_clerk_user_id IS NOT NULL
GROUP BY e.project_id, w.started_by_clerk_user_id
ON CONFLICT (project_id, member) DO NOTHING;
