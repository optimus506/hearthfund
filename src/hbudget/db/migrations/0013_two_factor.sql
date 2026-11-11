-- 0013: optional two-factor sign-in (D-095). Off for every existing user: the columns start empty.
-- totp_secret is the authenticator key (base32). It is set when enrolment starts and counts only once
-- totp_enabled_at is set. totp_last_step is the last 30-second step a code was accepted for (no replays).
-- While enrolment is pending, totp_pending_session is the hash of the session that gave the password and
-- totp_pending_at when: only that session, for a few minutes, sees the key and can confirm it.

ALTER TABLE users ADD COLUMN totp_secret TEXT;
ALTER TABLE users ADD COLUMN totp_enabled_at TEXT;
ALTER TABLE users ADD COLUMN totp_last_step INTEGER;
ALTER TABLE users ADD COLUMN totp_pending_session TEXT;
ALTER TABLE users ADD COLUMN totp_pending_at TEXT;

-- One-time recovery codes, stored hashed. A used code keeps its row (used_at) so the count left can be shown.
CREATE TABLE recovery_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX recovery_codes_user ON recovery_codes (user_id);

-- The step between the password and the code: a short-lived token in its own cookie. It is not a session and
-- opens no page; it only says "this browser gave the right password for this user a moment ago".
CREATE TABLE login_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
