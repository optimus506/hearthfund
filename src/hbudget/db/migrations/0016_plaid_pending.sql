-- 0016: pending transactions Plaid reported, kept only to explain a gap between the bank's balance and the
-- checkbook after a sync (docs/PLAID.md). The amount and date only: no description. A row leaves when Plaid
-- withdraws the pending transaction, which it does when the transaction posts. Never part of a backup or export.
CREATE TABLE plaid_pending (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    plaid_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    posted TEXT NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (household_id, plaid_id)
);
