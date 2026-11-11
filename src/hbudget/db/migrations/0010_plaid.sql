-- 0010: the optional Plaid bank connection (docs/PLAID.md). Empty unless a household turns it on. The access
-- token is stored sealed (AES-GCM, key derived from the server's secret key) and never leaves the server: no
-- backup, export or API answer includes these tables.

CREATE TABLE plaid_households (
    household_id INTEGER PRIMARY KEY REFERENCES households(id) ON DELETE CASCADE,
    enabled_at TEXT NOT NULL,
    enabled_by TEXT
);

CREATE TABLE plaid_items (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    institution TEXT NOT NULL DEFAULT '',
    token_sealed TEXT NOT NULL,
    cursor TEXT,
    linked_at TEXT NOT NULL,
    linked_by TEXT,
    last_sync_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, item_id)
);

CREATE TABLE plaid_accounts (
    household_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    mask TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    subtype TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'ignore' CHECK (role IN ('bank', 'card', 'ignore')),
    current INTEGER,
    PRIMARY KEY (household_id, account_id),
    FOREIGN KEY (household_id, item_id) REFERENCES plaid_items (household_id, item_id) ON DELETE CASCADE
);

-- every posted transaction Plaid sent, with the id it is stored under in bank_txns or card_txns (txn_id): the
-- bank's own posting from an earlier file upload when one has the same date and amount, else 'plaid:<id>'
CREATE TABLE plaid_txns (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    plaid_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    txn_id TEXT,
    posted TEXT NOT NULL,
    authorized TEXT,
    amount INTEGER NOT NULL,
    description TEXT NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (household_id, plaid_id)
);
CREATE INDEX plaid_txns_account ON plaid_txns (household_id, account_id, posted);
