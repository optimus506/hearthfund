-- 0014: wrong two-factor codes per user (D-097). After CODE_FAILURES of them in a day, codes from the app are
-- refused for that user until the count drops; recovery codes still work. A right code clears them.
CREATE TABLE code_failures (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    at TEXT NOT NULL
);
CREATE INDEX code_failures_user ON code_failures (user_id, at);
