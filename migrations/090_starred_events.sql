-- Someone who starred one of the project's GitHub repositories gets a `starred` event on their
-- record, dated when GitHub says they starred it. Only the allowed kinds change.
ALTER TABLE record_events DROP CONSTRAINT IF EXISTS record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'viewed', 'visited', 'invited', 'accepted', 'withdrawn',
                    'connected', 'starred'));
