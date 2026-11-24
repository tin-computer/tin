-- A member's own labels on people, such as the outreach group someone was added in, so the
-- team can compare groups. Each tag is a short label; a person has any number.
CREATE TABLE IF NOT EXISTS record_tags (
    project_id uuid NOT NULL,
    record_id uuid NOT NULL,
    tag text NOT NULL CHECK (length(tag) BETWEEN 1 AND 60 AND tag = btrim(tag)),
    added_by text NOT NULL CHECK (length(added_by) BETWEEN 1 AND 200),
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, tag),
    FOREIGN KEY (project_id, record_id) REFERENCES records (project_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS record_tags_project ON record_tags (project_id, tag);

-- A connection request may name someone known only by their profile link: the sending run looks
-- up their profile ID (ACo…) right before it sends.
ALTER TABLE linkedin_requests ALTER COLUMN target DROP NOT NULL;
ALTER TABLE linkedin_requests ADD COLUMN IF NOT EXISTS slug text
    CHECK (slug IS NULL OR length(slug) BETWEEN 1 AND 300);
ALTER TABLE linkedin_requests DROP CONSTRAINT IF EXISTS linkedin_requests_named;
ALTER TABLE linkedin_requests ADD CONSTRAINT linkedin_requests_named
    CHECK (target IS NOT NULL OR slug IS NOT NULL);
