-- 0012: an optional daily sync for the Plaid connection (docs/PLAID.md). 'off' for every existing household: the
-- daily sync only stores what Plaid sent; the check-in still turns it into an upload.

ALTER TABLE plaid_households ADD COLUMN auto_sync TEXT NOT NULL DEFAULT 'off' CHECK (auto_sync IN ('off', 'daily'));
