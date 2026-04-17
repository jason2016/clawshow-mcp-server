-- =============================================================
-- ClawShow SaaS Transformation Migration
-- 2026-04-17
-- Note: WAL mode already enabled, skip.
-- Note: namespaces table already exists, ALTER handled by Python script.
-- =============================================================

-- =============================================================
-- Users: email-based identity
-- =============================================================
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  email_verified BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- =============================================================
-- User-Namespace membership (many-to-many)
-- One user can own multiple namespaces (businesses)
-- One namespace can have multiple users (Phase 2 team support)
-- =============================================================
CREATE TABLE IF NOT EXISTS user_namespaces (
  user_id TEXT REFERENCES users(id),
  namespace TEXT REFERENCES namespaces(namespace),
  role TEXT DEFAULT 'admin',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, namespace)
);
CREATE INDEX IF NOT EXISTS idx_user_namespaces_user ON user_namespaces(user_id);
CREATE INDEX IF NOT EXISTS idx_user_namespaces_ns ON user_namespaces(namespace);

-- =============================================================
-- Usage events: per-envelope audit trail
-- =============================================================
CREATE TABLE IF NOT EXISTS usage_events (
  id TEXT PRIMARY KEY,
  namespace TEXT REFERENCES namespaces(namespace),
  esign_document_id TEXT,
  event_type TEXT NOT NULL,
  billed BOOLEAN DEFAULT FALSE,
  is_overage BOOLEAN DEFAULT FALSE,
  overage_amount_cents INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_usage_ns_created ON usage_events(namespace, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_billed ON usage_events(billed, is_overage);

-- =============================================================
-- API Keys (one key belongs to one namespace)
-- =============================================================
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  namespace TEXT REFERENCES namespaces(namespace),
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  name TEXT,
  last_used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  revoked_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_ns ON api_keys(namespace);

-- =============================================================
-- Magic link login tokens
-- =============================================================
CREATE TABLE IF NOT EXISTS login_tokens (
  token TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_login_tokens_email ON login_tokens(email);

-- =============================================================
-- Sessions (dashboard login persistence)
-- =============================================================
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
