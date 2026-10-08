-- website.change: the copy fix qa.feedback_to_fix planned becomes a change row.
--
-- One 'feedback' row of kind 'copy' per planned fix; approving it starts website.change, which
-- applies the plan's files as they are.
ALTER TABLE website_changes DROP CONSTRAINT website_changes_source_check;
ALTER TABLE website_changes ADD CONSTRAINT website_changes_source_check
    CHECK (source IN ('audit', 'planned', 'blog_index', 'feedback'));
