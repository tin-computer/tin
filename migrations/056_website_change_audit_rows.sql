-- website.change, phase 2: the audit's findings become change rows.
--
-- A row from the latest organic audit has source 'audit', its stable finding ID (oa_…) as its
-- change ID and the site-fix-v5 repair it plans as its kind. This replaces the placeholder
-- 'technical_fix' source from migration 053, which no code ever proposed: the audit finds,
-- and website.change plans, fixes and publishes.
ALTER TABLE website_changes DROP CONSTRAINT website_changes_source_check;
ALTER TABLE website_changes ADD CONSTRAINT website_changes_source_check
    CHECK (source IN ('audit', 'planned_url_change', 'blog_index'));
