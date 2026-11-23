-- A company-only RB2B visit: the ICP model's verdict on the company (worth chasing, fit,
-- intent, reason, the titles to look for), the People records of whom Tin found there, and
-- what Delegate charged to find them. Null until the run judges the company.
ALTER TABLE rb2b_visits ADD COLUMN IF NOT EXISTS company jsonb;
