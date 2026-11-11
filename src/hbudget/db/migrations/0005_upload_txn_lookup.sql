-- 0005: look up an upload link by transaction. The primary keys start with (household_id, upload_id), so
-- "which uploads contain this transaction" (the history filter run by the summary, report and check-up, and
-- the cascade when a transaction is deleted) could only narrow by household and scanned every link row.
-- Adds no columns; drops nothing.

CREATE INDEX upload_txns_txn ON upload_txns (household_id, txn_id, upload_id);
CREATE INDEX card_upload_txns_txn ON card_upload_txns (household_id, txn_id, upload_id);
