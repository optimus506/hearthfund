-- 0018: a linked account can keep the balance of an investment account on Money > Investments up to date (D-102).
-- `tracks` is that account's id (remembered when tracking is switched off, so switching it on again does not make
-- a second account); `tracking` says whether each sync notes the balance. Off for every existing account.
ALTER TABLE plaid_accounts ADD COLUMN tracks TEXT;
ALTER TABLE plaid_accounts ADD COLUMN tracking INTEGER NOT NULL DEFAULT 0;
