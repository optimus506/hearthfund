-- 0001: the initial schema (ARCHITECTURE 14.2). Money is INTEGER cents, dates TEXT ISO, booleans INTEGER 0/1,
-- JSON columns TEXT. Every household table is keyed by (household_id, id).

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    password_changed_at TEXT
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE,
    ip TEXT NOT NULL,
    at TEXT NOT NULL,
    ok INTEGER NOT NULL
);

CREATE TABLE login_backoff (
    kind TEXT NOT NULL CHECK (kind IN ('user', 'ip')),
    key TEXT NOT NULL COLLATE NOCASE,
    reject_until TEXT NOT NULL,
    failures INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE households (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    start_balance INTEGER NOT NULL DEFAULT 0,
    as_of TEXT NOT NULL,
    last_reconciled_through TEXT,
    horizon_mode TEXT NOT NULL DEFAULT 'rolling' CHECK (horizon_mode IN ('rolling', 'fixed')),
    horizon_months INTEGER NOT NULL DEFAULT 15,
    horizon_end TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE memberships (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'contributor', 'viewer')),
    PRIMARY KEY (user_id, household_id)
);

CREATE TABLE series (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'bill', 'spending')),
    amount INTEGER NOT NULL,
    schedule_json TEXT NOT NULL,
    funded_by TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    card TEXT,
    grp TEXT,
    month_amounts_json TEXT NOT NULL DEFAULT '[]',
    cycle_amounts_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    generated_through TEXT,
    PRIMARY KEY (household_id, id)
);

CREATE TABLE ledger_rows (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    series_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'bill', 'spending')),
    amount INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    nominal_date TEXT,
    order_date TEXT NOT NULL,
    seq INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'cleared', 'removed')),
    status_override TEXT,
    amount_was INTEGER,
    overridden INTEGER NOT NULL DEFAULT 0,
    card TEXT,
    paid INTEGER NOT NULL DEFAULT 0,
    paid_txn_ids_json TEXT NOT NULL DEFAULT '[]',
    paid_at_change INTEGER NOT NULL DEFAULT 0,
    cleared_txn_id TEXT,
    cleared_on TEXT,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, id)
);
CREATE INDEX ledger_rows_order ON ledger_rows (household_id, order_date, seq);

CREATE TABLE payee_rules (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    contains_json TEXT NOT NULL DEFAULT '[]',
    regex TEXT,
    direction TEXT NOT NULL DEFAULT 'any',
    min_amount INTEGER,
    max_amount INTEGER,
    targets_json TEXT NOT NULL DEFAULT '[]',
    card TEXT,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, id)
);

CREATE TABLE accounts (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    apr_bp INTEGER NOT NULL DEFAULT 0,
    payment INTEGER NOT NULL DEFAULT 0,
    minimum INTEGER NOT NULL DEFAULT 0,
    credit_limit INTEGER,
    as_of TEXT,
    promo_ends TEXT,
    apr_after_bp INTEGER,
    deferred_interest INTEGER NOT NULL DEFAULT 0,
    is_mortgage INTEGER NOT NULL DEFAULT 0,
    include_in_payoff INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, id)
);

CREATE TABLE expected_items (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL,
    amount INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    PRIMARY KEY (household_id, id)
);

CREATE TABLE card_categories (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    bank_categories_json TEXT NOT NULL DEFAULT '[]',
    weekly_series_json TEXT NOT NULL DEFAULT '[]',
    has_own_rows INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (household_id, name)
);

CREATE TABLE flow_groups (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    colour TEXT NOT NULL DEFAULT '--f-other',
    rule_ids_json TEXT NOT NULL DEFAULT '[]',
    series_ids_json TEXT NOT NULL DEFAULT '[]',
    row_names_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (household_id, name)
);

CREATE TABLE uploads (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('bank', 'card')),
    filename TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT,
    rows INTEGER NOT NULL DEFAULT 0,
    date_from TEXT,
    date_to TEXT,
    latest_balance INTEGER,
    has_balance INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'imported', 'rejected')),
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, id)
);

CREATE TABLE bank_txns (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    first_upload_id TEXT,
    posted TEXT NOT NULL,
    effective TEXT,
    amount INTEGER NOT NULL,
    balance INTEGER,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    file_index INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (household_id, id)
);

CREATE TABLE upload_txns (
    household_id INTEGER NOT NULL,
    upload_id TEXT NOT NULL,
    txn_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    PRIMARY KEY (household_id, upload_id, txn_id),
    FOREIGN KEY (household_id, txn_id) REFERENCES bank_txns (household_id, id) ON DELETE CASCADE,
    FOREIGN KEY (household_id, upload_id) REFERENCES uploads (household_id, id) ON DELETE CASCADE
);

CREATE TABLE card_txns (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    first_upload_id TEXT,
    purchased TEXT NOT NULL,
    posted TEXT NOT NULL,
    amount INTEGER NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (household_id, id)
);

CREATE TABLE mapping_profiles (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('bank', 'card', 'workbook')),
    header_json TEXT NOT NULL DEFAULT '[]',
    mapping_json TEXT NOT NULL,
    PRIMARY KEY (household_id, id)
);

CREATE TABLE reconciliations (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    at TEXT NOT NULL,
    by_user TEXT,
    upload_id TEXT,
    through TEXT NOT NULL,
    new_balance INTEGER NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    report_md TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (household_id, id)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    household_id INTEGER,                 -- NULL for server-wide events; no FK: kept when a household goes
    at TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    row_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    imported INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
