-- Tin's own top-ups can use Stripe live mode (TIN_LITE_STRIPE_MODE).
--
-- Balances carry over unchanged: an account opened in test mode keeps its welcome credit
-- and earlier top-ups. Each payment records the Stripe mode it was made in, because its
-- session and payment intent exist only there; a test payment is never refunded, read or
-- reconciled through a live key.
ALTER TABLE billing_accounts DROP CONSTRAINT billing_accounts_mode_check;
ALTER TABLE billing_accounts ADD CONSTRAINT billing_accounts_mode_check
    CHECK (mode IN ('test', 'live'));

ALTER TABLE billing_payments ADD COLUMN mode text NOT NULL DEFAULT 'test'
    CHECK (mode IN ('test', 'live'));
ALTER TABLE billing_payments ALTER COLUMN mode DROP DEFAULT;
