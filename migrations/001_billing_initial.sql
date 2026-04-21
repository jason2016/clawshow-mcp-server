-- ClawShow Billing MVP — Initial Schema
-- Week 1 (2026-04-21)
-- Run via: python -c "from storage.billing_db import BillingDB; BillingDB().init_tables()"

CREATE TABLE IF NOT EXISTS billing_plans (
    plan_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,

    customer_email TEXT NOT NULL,
    customer_name TEXT,
    customer_phone TEXT,

    total_amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    installments INTEGER NOT NULL,
    frequency TEXT NOT NULL,
    start_date DATE NOT NULL,

    gateway TEXT NOT NULL,
    gateway_plan_id TEXT,
    gateway_customer_id TEXT,
    gateway_mandate_id TEXT,
    gateway_mode TEXT DEFAULT 'test',

    -- eSign Option A (Phase 1: client provides PDF)
    contract_required BOOLEAN DEFAULT FALSE,
    contract_pdf_url TEXT,
    contract_esign_request_id TEXT,
    contract_signed_at TIMESTAMP,

    -- eSign Option B (Phase 2: ClawShow generates from template)
    contract_template TEXT,
    contract_variables TEXT,

    -- External platform sync (webhook-based)
    external_platform_name TEXT,
    external_webhook_url TEXT,
    external_order_id TEXT,
    external_auth_token TEXT,

    status TEXT NOT NULL DEFAULT 'pending',
    description TEXT,
    metadata TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_plans_namespace ON billing_plans(namespace);
CREATE INDEX IF NOT EXISTS idx_plans_status ON billing_plans(status);


CREATE TABLE IF NOT EXISTS billing_installments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    installment_number INTEGER NOT NULL,

    amount REAL NOT NULL,
    scheduled_date DATE NOT NULL,

    status TEXT NOT NULL DEFAULT 'scheduled',

    gateway_payment_id TEXT,
    charged_at TIMESTAMP,
    failure_reason TEXT,
    failure_classification TEXT,

    retry_count INTEGER DEFAULT 0,
    last_retry_at TIMESTAMP,
    next_retry_at TIMESTAMP,

    FOREIGN KEY (plan_id) REFERENCES billing_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_installments_plan ON billing_installments(plan_id);
CREATE INDEX IF NOT EXISTS idx_installments_scheduled ON billing_installments(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_installments_status ON billing_installments(status);


CREATE TABLE IF NOT EXISTS billing_commissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    installment_id INTEGER,

    transaction_amount REAL NOT NULL,
    commission_rate REAL NOT NULL,
    commission_amount REAL NOT NULL,

    charged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (installment_id) REFERENCES billing_installments(id)
);

CREATE INDEX IF NOT EXISTS idx_commissions_namespace ON billing_commissions(namespace);


CREATE TABLE IF NOT EXISTS billing_webhook_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    payload TEXT NOT NULL,
    http_status INTEGER,
    response_body TEXT,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    succeeded BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_webhook_logs_plan ON billing_webhook_logs(plan_id);
