-- 0007: each account keeps its past balances (as of, balance) for the debt burn-down chart (D-077).
-- Adds a column with an empty list; changes nothing else.

ALTER TABLE accounts ADD COLUMN history_json TEXT NOT NULL DEFAULT '[]';
