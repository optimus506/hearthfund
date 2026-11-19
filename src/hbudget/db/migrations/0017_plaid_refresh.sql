-- 0017: when each linked bank was last asked for an on-demand refresh (docs/PLAID.md, D-101). The refresh is an
-- optional, paid Plaid add-on, off unless the server turns it on; the time keeps it to one request an hour per bank.
ALTER TABLE plaid_items ADD COLUMN last_refresh_at TEXT;
