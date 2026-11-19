-- 0021: Plaid Liabilities, off unless the server allows it and the owner turned it on for a bank (D-106):
-- whether a linked bank is asked for rates, minimum payments and due dates, why that did not work the last time,
-- and what came back per account (rate, minimum, next due date, statement balance; no account numbers).
ALTER TABLE plaid_items ADD COLUMN liabilities INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plaid_items ADD COLUMN liabilities_note TEXT NOT NULL DEFAULT '';
ALTER TABLE plaid_accounts ADD COLUMN liability_json TEXT;
