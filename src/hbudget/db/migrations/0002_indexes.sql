-- 0002: indexes for the history, uploads, audit and housekeeping queries. Adds no columns; drops nothing.

CREATE INDEX bank_txns_posted ON bank_txns (household_id, posted);
CREATE INDEX upload_txns_order ON upload_txns (household_id, upload_id, file_index);
CREATE INDEX card_txns_purchased ON card_txns (household_id, purchased);
CREATE INDEX audit_log_household ON audit_log (household_id, at);
CREATE INDEX login_attempts_at ON login_attempts (at);
CREATE INDEX login_attempts_user ON login_attempts (username, at);
CREATE INDEX login_attempts_ip ON login_attempts (ip, at);
CREATE INDEX sessions_user ON sessions (user_id);
CREATE INDEX sessions_expiry ON sessions (expires_at);
CREATE INDEX memberships_household ON memberships (household_id);
