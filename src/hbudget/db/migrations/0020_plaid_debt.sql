-- 0020: a linked account can keep the balance of a debt on the Debts page (D-105). Independent of the
-- account's role: a card linked for its purchases can keep its debt's balance too.
ALTER TABLE plaid_accounts ADD COLUMN debt TEXT;
