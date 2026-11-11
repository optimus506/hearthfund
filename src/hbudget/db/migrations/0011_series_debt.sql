-- 0011: a recurring item can name the debt its payments pay down (#97), for the estimated balance between hand
-- updates on the Debts page. Empty for every existing item; nothing else changes.

ALTER TABLE series ADD COLUMN debt TEXT;
