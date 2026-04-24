-- eSign Users (passwordless)
CREATE TABLE IF NOT EXISTS esign_users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT NOT NULL UNIQUE,
    display_name      TEXT,
    account_type      TEXT NOT NULL DEFAULT 'personal'
                          CHECK(account_type IN ('personal', 'organization')),
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK(status IN ('active', 'suspended', 'deleted')),
    free_quota_total  INTEGER NOT NULL DEFAULT 3,
    free_quota_used   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now', 'utc')),
    last_login_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_esign_users_email ON esign_users(email);

-- Login OTPs (separate from signing OTPs in esign_otp)
CREATE TABLE IF NOT EXISTS esign_login_otps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL,
    otp_code     TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now', 'utc')),
    expires_at   TEXT NOT NULL,
    verified_at  TEXT,
    attempts     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_esign_login_otps_email ON esign_login_otps(email);

-- User sessions (cookie-based)
CREATE TABLE IF NOT EXISTS esign_user_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    session_token  TEXT NOT NULL UNIQUE,
    created_at     TEXT DEFAULT (datetime('now', 'utc')),
    expires_at     TEXT NOT NULL,
    last_used_at   TEXT,
    ip_address     TEXT,
    user_agent     TEXT,
    FOREIGN KEY (user_id) REFERENCES esign_users(id)
);

CREATE INDEX IF NOT EXISTS idx_esign_user_sessions_token ON esign_user_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_esign_user_sessions_user  ON esign_user_sessions(user_id);

-- Link documents to creators
ALTER TABLE esign_documents ADD COLUMN creator_user_id INTEGER
    REFERENCES esign_users(id);

CREATE INDEX IF NOT EXISTS idx_esign_docs_creator ON esign_documents(creator_user_id);
