-- The GitHub account a Tin user authorized while connecting GitHub to a project. Only the
-- numeric id (stable across renames) and the current login are kept; the user token is not.
-- The contributor check reads this to match a pull-request author to a Tin user.
CREATE TABLE tin_user_github_identities (
    clerk_user_id text PRIMARY KEY CHECK (clerk_user_id ~ '^user_[A-Za-z0-9]+$'),
    github_user_id bigint NOT NULL UNIQUE CHECK (github_user_id > 0),
    github_login text NOT NULL CHECK (length(github_login) BETWEEN 1 AND 39),
    linked_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
