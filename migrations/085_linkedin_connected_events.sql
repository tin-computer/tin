-- A member's own LinkedIn connections (social.linkedin_connections) get a `connected` event on
-- their record, dated when LinkedIn says they connected. Only the allowed kinds change.
ALTER TABLE record_events DROP CONSTRAINT IF EXISTS record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'viewed', 'visited', 'invited', 'accepted', 'withdrawn',
                    'connected'));
