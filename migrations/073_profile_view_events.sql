-- People who viewed a member's LinkedIn profile (social.linkedin_profile_viewers) get a `viewed`
-- event on their record, like a reaction to the member's post. Only the allowed kinds change.
ALTER TABLE record_events DROP CONSTRAINT record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'viewed', 'invited', 'accepted', 'withdrawn'));
