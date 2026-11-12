-- The one LinkedIn post a project has up next for its posting slots. A member picks it from an
-- exact preview: the approval it names freezes the text, images, first comment and account.
-- Picking another post replaces the row; the slot that posts it, a change to the post, or a
-- member taking it out deletes it.
CREATE TABLE linkedin_up_next (
  project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  approval_id uuid NOT NULL,
  draft_path text NOT NULL CHECK (length(draft_path) BETWEEN 1 AND 512),
  post_id text NOT NULL CHECK (post_id ~ '^p[1-6]$'),
  fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
  picked_by_clerk_user_id text NOT NULL,
  picked_at timestamptz NOT NULL DEFAULT now()
);
