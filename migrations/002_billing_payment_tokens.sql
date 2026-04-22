-- ClawShow Billing — Payment Tokens (Week 1 / P0-1)
-- Generated: 2026-04-22
-- Each token gives a customer access to pay one specific installment.

CREATE TABLE IF NOT EXISTS billing_payment_tokens (
    token TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    installment_no INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,           -- set when payment is initiated (prevents replay)
    paid_at TIMESTAMP,           -- set when gateway confirms payment
    last_accessed_at TIMESTAMP,
    access_count INTEGER DEFAULT 0,

    -- last gateway payment created via this token
    gateway_payment_id TEXT,

    FOREIGN KEY (plan_id) REFERENCES billing_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_btokens_plan      ON billing_payment_tokens(plan_id);
CREATE INDEX IF NOT EXISTS idx_btokens_expires   ON billing_payment_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_btokens_namespace ON billing_payment_tokens(namespace);
