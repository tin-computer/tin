-- website.change, phase 3: planned URL changes and the blog index become change rows.
--
-- Rows from page decisions and the site architecture plan take source 'planned' (kind
-- 'redirect' or 'noindex'), replacing the 'planned_url_change' placeholder no code proposed.
-- A blog index plan is one 'blog_index' row of kind 'index'.
ALTER TABLE website_changes DROP CONSTRAINT website_changes_source_check;
ALTER TABLE website_changes ADD CONSTRAINT website_changes_source_check
    CHECK (source IN ('audit', 'planned', 'blog_index'));
