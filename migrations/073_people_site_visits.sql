-- People records learn the people who used a project's site or product (product.visitor_journeys):
-- an email they gave the product recognizes them, and a `visited` event on their timeline holds
-- what they did that day. Anonymous visitors never get a record.
ALTER TABLE record_identifiers DROP CONSTRAINT IF EXISTS record_identifiers_kind_check;
ALTER TABLE record_identifiers ADD CONSTRAINT record_identifiers_kind_check
    CHECK (kind IN ('linkedin_member', 'linkedin_slug', 'email'));

ALTER TABLE record_events DROP CONSTRAINT IF EXISTS record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'invited', 'accepted', 'withdrawn', 'visited'));
