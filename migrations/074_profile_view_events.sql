-- People who viewed a member's LinkedIn profile (social.linkedin_profile_viewers) get a `viewed`
-- event on their record, like a reaction to the member's post. Only the allowed kinds change.
-- `visited` (site visits in People, from its own migration) is allowed too, so this holds
-- whichever of the two lands first.
ALTER TABLE record_events DROP CONSTRAINT IF EXISTS record_events_kind_check;
ALTER TABLE record_events ADD CONSTRAINT record_events_kind_check
    CHECK (kind IN ('reacted', 'commented', 'viewed', 'visited', 'invited', 'accepted', 'withdrawn'));
