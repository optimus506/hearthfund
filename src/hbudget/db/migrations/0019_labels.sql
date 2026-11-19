-- 0019: labels on recurring items and on rows, and items that need no scheduling (D-104).
ALTER TABLE series ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE series ADD COLUMN no_schedule INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ledger_rows ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]';
