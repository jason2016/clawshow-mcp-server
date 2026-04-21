"""
ClawShow Billing SQLite layer.
DB file: data/billing.db
All times UTC. Namespace isolation enforced on every query.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent.parent / "data" / "billing.db"
SCHEMA_PATH = Path(__file__).parent.parent / "migrations" / "001_billing_initial.sql"


def _ensure_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class BillingDB:

    def init_tables(self) -> None:
        sql = SCHEMA_PATH.read_text()
        with get_conn() as conn:
            conn.executescript(sql)

    # ------------------------------------------------------------------ plans

    def create_plan(self, plan: dict) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO billing_plans (
                    plan_id, namespace, customer_email, customer_name, customer_phone,
                    total_amount, currency, installments, frequency, start_date,
                    gateway, gateway_plan_id, gateway_customer_id, gateway_mandate_id, gateway_mode,
                    contract_required, contract_pdf_url, contract_esign_request_id,
                    contract_template, contract_variables,
                    external_platform_name, external_webhook_url, external_order_id, external_auth_token,
                    status, description, metadata
                ) VALUES (
                    :plan_id, :namespace, :customer_email, :customer_name, :customer_phone,
                    :total_amount, :currency, :installments, :frequency, :start_date,
                    :gateway, :gateway_plan_id, :gateway_customer_id, :gateway_mandate_id, :gateway_mode,
                    :contract_required, :contract_pdf_url, :contract_esign_request_id,
                    :contract_template, :contract_variables,
                    :external_platform_name, :external_webhook_url, :external_order_id, :external_auth_token,
                    :status, :description, :metadata
                )
                """,
                plan,
            )

    def get_plan(self, plan_id: str, namespace: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM billing_plans WHERE plan_id = ? AND namespace = ?",
                (plan_id, namespace),
            ).fetchone()
        return dict(row) if row else None

    def update_plan_status(self, plan_id: str, namespace: str, status: str, **extra: Any) -> None:
        fields = {"status": status, "updated_at": _now(), **extra}
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        fields["plan_id"] = plan_id
        fields["namespace"] = namespace
        with get_conn() as conn:
            conn.execute(
                f"UPDATE billing_plans SET {sets} WHERE plan_id = :plan_id AND namespace = :namespace",
                fields,
            )

    def list_plans(self, namespace: str, status: str | None = None) -> list[dict]:
        with get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM billing_plans WHERE namespace = ? AND status = ? ORDER BY created_at DESC",
                    (namespace, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM billing_plans WHERE namespace = ? ORDER BY created_at DESC",
                    (namespace,),
                ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- installments

    def create_installments(self, installments: list[dict]) -> None:
        with get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO billing_installments
                    (plan_id, installment_number, amount, scheduled_date, status)
                VALUES
                    (:plan_id, :installment_number, :amount, :scheduled_date, :status)
                """,
                installments,
            )

    def get_installments(self, plan_id: str) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM billing_installments WHERE plan_id = ? ORDER BY installment_number",
                (plan_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_installment(self, installment_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        fields["id"] = installment_id
        with get_conn() as conn:
            conn.execute(
                f"UPDATE billing_installments SET {sets} WHERE id = :id",
                fields,
            )

    # -------------------------------------------------------------- commissions

    def record_commission(self, commission: dict) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO billing_commissions
                    (namespace, plan_id, installment_id, transaction_amount, commission_rate, commission_amount)
                VALUES
                    (:namespace, :plan_id, :installment_id, :transaction_amount, :commission_rate, :commission_amount)
                """,
                commission,
            )

    # -------------------------------------------------------------- webhook logs

    def log_webhook(self, log: dict) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO billing_webhook_logs
                    (plan_id, event_type, webhook_url, payload, http_status, response_body, succeeded)
                VALUES
                    (:plan_id, :event_type, :webhook_url, :payload, :http_status, :response_body, :succeeded)
                """,
                log,
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
