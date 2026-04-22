-- Migration 003: FocusingPro Writeback fields on billing_installments
-- Applied: 2026-04-22

ALTER TABLE billing_installments ADD COLUMN focusingpro_record_id TEXT;
ALTER TABLE billing_installments ADD COLUMN writeback_status TEXT;
-- values: null (not attempted) | "success" | "failed" | "skipped"

ALTER TABLE billing_installments ADD COLUMN writeback_error TEXT;
ALTER TABLE billing_installments ADD COLUMN writeback_steps_completed TEXT;
-- comma-separated: "find,register,confirm"

ALTER TABLE billing_installments ADD COLUMN writeback_attempted_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_installments_writeback
    ON billing_installments(writeback_status)
    WHERE writeback_status IS NOT NULL;
