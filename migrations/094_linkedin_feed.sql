-- social.linkedin_feed: posts from members' LinkedIn home feeds.
-- - linkedin_feed_posts: one row per post a member's feed showed in a project, named by its
--   thread (`activity:<id>` or `ugcPost:<id>`). `post` is the post as last read (author, words,
--   media, counts); `comments` and `reactors` are its first comments and reactors, read once.
-- - linkedin_feed_sightings: which member's feed showed it, in which run, where and why.
CREATE TABLE IF NOT EXISTS linkedin_feed_posts (
    project_id uuid NOT NULL REFERENCES projects(id),
    thread text NOT NULL,
    post jsonb NOT NULL,
    comments jsonb,
    comments_total integer,
    reactors jsonb,
    reactors_total integer,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    details_read_at timestamptz,
    PRIMARY KEY (project_id, thread)
);

CREATE TABLE IF NOT EXISTS linkedin_feed_sightings (
    run_id uuid NOT NULL,
    thread text NOT NULL,
    project_id uuid NOT NULL,
    member text NOT NULL,
    position integer NOT NULL,
    reason text,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, thread),
    FOREIGN KEY (project_id, thread) REFERENCES linkedin_feed_posts (project_id, thread)
);

CREATE INDEX IF NOT EXISTS linkedin_feed_sightings_member
    ON linkedin_feed_sightings (project_id, member, seen_at DESC);
