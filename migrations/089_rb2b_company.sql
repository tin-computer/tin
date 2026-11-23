-- A company-only RB2B visit: the ICP model's verdict on the company (worth chasing, fit,
-- intent, reason, the titles to look for), the People records of whom Tin found there, and
-- what Delegate charged to find them. Null until the run judges the company.
ALTER TABLE rb2b_visits ADD COLUMN IF NOT EXISTS company jsonb;

-- A visit Tin keeps but never starts a run for: one over a project's daily limit of company
-- runs, and every company visit received before company visits started runs (no backfill of
-- Tin-funded work nobody asked for).
ALTER TABLE rb2b_visits ADD COLUMN IF NOT EXISTS no_start boolean NOT NULL DEFAULT false;
-- When a company visit took one of the day's runs: counted under a per-project lock, so two
-- visits at once can't both take the last one.
ALTER TABLE rb2b_visits ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
UPDATE rb2b_visits SET no_start = true WHERE kind = 'company' AND run_id IS NULL;
