-- Managed clients Tin bills outside Stripe. An invoiced workspace starts paid runs without
-- prepaid credits: usage is still metered and charged to its ledger, so its balance can fall
-- below zero, and an operator records each invoice payment as an adjustment. Project spending
-- limits still apply. Only the operator CLI changes this; no dashboard or MCP control does.
ALTER TABLE billing_accounts ADD COLUMN funding text NOT NULL DEFAULT 'prepaid'
    CHECK (funding IN ('prepaid', 'invoiced'));

-- Who changed a wallet's funding, when and why. Append-only, like the ledger.
CREATE TABLE billing_funding_changes (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES billing_accounts(workspace_id),
    funding text NOT NULL CHECK (funding IN ('prepaid', 'invoiced')),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    operator text NOT NULL CHECK (length(operator) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_funding_changes_workspace_idx
    ON billing_funding_changes(workspace_id, created_at DESC);
CREATE TRIGGER billing_funding_changes_immutable BEFORE UPDATE OR DELETE
    ON billing_funding_changes FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only();
