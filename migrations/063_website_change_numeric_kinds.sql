-- Repair kinds such as soft_404 contain digits; source/kind validation stays in ChangeRow.
ALTER TABLE website_changes DROP CONSTRAINT website_changes_kind_check;
ALTER TABLE website_changes ADD CONSTRAINT website_changes_kind_check
    CHECK (kind ~ '^[a-z][a-z0-9_]{1,39}$');
