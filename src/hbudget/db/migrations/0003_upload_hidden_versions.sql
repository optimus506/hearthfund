-- 0003: hidden uploads (D-028), card upload links, a budget version for optimistic edits, and restored audit
-- usernames kept as text. Adds columns and a table; drops nothing.

ALTER TABLE uploads ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE households ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audit_log ADD COLUMN by_name TEXT;

CREATE TABLE card_upload_txns (
    household_id INTEGER NOT NULL,
    upload_id TEXT NOT NULL,
    txn_id TEXT NOT NULL,
    PRIMARY KEY (household_id, upload_id, txn_id),
    FOREIGN KEY (household_id, txn_id) REFERENCES card_txns (household_id, id) ON DELETE CASCADE,
    FOREIGN KEY (household_id, upload_id) REFERENCES uploads (household_id, id) ON DELETE CASCADE
);
INSERT INTO card_upload_txns (household_id, upload_id, txn_id)
    SELECT c.household_id, c.first_upload_id, c.id FROM card_txns c
    JOIN uploads u ON u.household_id = c.household_id AND u.id = c.first_upload_id;
