-- 0006: access tokens for the versioned API and the MCP endpoint (D-068). Adds a table; changes nothing else.
-- Only a SHA-256 of each token is stored; the token itself is shown once, when it is made.

CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    token_hash TEXT NOT NULL UNIQUE,
    hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX api_tokens_household ON api_tokens (household_id, id);
