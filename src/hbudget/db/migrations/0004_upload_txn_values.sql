-- 0004: every upload keeps its own file's values for a transaction (D-047). Two exports of the same postings can
-- carry different running balances (a corrected re-export); the preview of an upload must use its own file.
-- Adds columns; back-fills them from bank_txns so existing previews keep working; drops nothing.

ALTER TABLE upload_txns ADD COLUMN balance INTEGER;
ALTER TABLE upload_txns ADD COLUMN category TEXT NOT NULL DEFAULT '';
UPDATE upload_txns SET
    balance = (SELECT b.balance FROM bank_txns b WHERE b.household_id = upload_txns.household_id
               AND b.id = upload_txns.txn_id),
    category = COALESCE((SELECT b.category FROM bank_txns b WHERE b.household_id = upload_txns.household_id
                         AND b.id = upload_txns.txn_id), '');
