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

    def get_conn_ctx(self):
        """Expose get_conn for use in adapter helpers."""
        return get_conn()

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

    def get_installment(self, installment_id: int) -> dict | None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM billing_installments WHERE id = ?", (installment_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_installment_by_gateway_payment(self, gateway_payment_id: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM billing_installments WHERE gateway_payment_id = ?",
                (gateway_payment_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_charged_installments(self, plan_id: str) -> int:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM billing_installments WHERE plan_id = ? AND status = 'charged'",
                (plan_id,),
            ).fetchone()
        return row[0] if row else 0

    def generate_next_preview_installment(self, plan_id: str) -> None:
        """For infinite subscriptions: after a charge, append next preview row."""
        with get_conn() as conn:
            last = conn.execute(
                """SELECT installment_number, scheduled_date, amount
                   FROM billing_installments WHERE plan_id = ?
                   ORDER BY installment_number DESC LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if not last:
                return
            from dateutil.relativedelta import relativedelta
            import datetime as dt
            plan_row = conn.execute(
                "SELECT frequency FROM billing_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if not plan_row:
                return
            freq = plan_row["frequency"]
            last_date = dt.date.fromisoformat(last["scheduled_date"])
            if freq == "monthly":
                next_date = last_date + relativedelta(months=1)
            elif freq == "quarterly":
                next_date = last_date + relativedelta(months=3)
            elif freq == "weekly":
                next_date = last_date + dt.timedelta(weeks=1)
            else:
                return
            conn.execute(
                """INSERT INTO billing_installments
                   (plan_id, installment_number, amount, scheduled_date, status)
                   VALUES (?, ?, ?, ?, 'scheduled')""",
                (plan_id, last["installment_number"] + 1, last["amount"], next_date.isoformat()),
            )

    def update_installment(self, installment_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        fields["id"] = installment_id
        with get_conn() as conn:
            conn.execute(
                f"UPDATE billing_installments SET {sets} WHERE id = :id",
                fields,
            )

    def cancel_pending_installments(self, plan_id: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE billing_installments SET status = 'cancelled' WHERE plan_id = ? AND status = 'scheduled'",
                (plan_id,),
            )

    def find_plan_by_id_any_namespace(self, plan_id: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM billing_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_plan(self, plan_id: str, namespace: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        fields["plan_id"] = plan_id
        fields["namespace"] = namespace
        fields.setdefault("updated_at", _now())
        with get_conn() as conn:
            conn.execute(
                f"UPDATE billing_plans SET {sets}, updated_at = :updated_at WHERE plan_id = :plan_id AND namespace = :namespace",
                fields,
            )

    def update_installment_by_gateway_payment(
        self, gateway_payment_id: str, subscription_id: str, status: str
    ) -> None:
        """Called from Mollie webhook: match installment by subscription_id, update status."""
        now = _now()
        with get_conn() as conn:
            # Find plan by gateway_plan_id (subscription_id)
            plan = conn.execute(
                "SELECT plan_id FROM billing_plans WHERE gateway_plan_id = ?",
                (subscription_id,),
            ).fetchone()
            if not plan:
                return
            plan_id = plan["plan_id"]
            # Find earliest scheduled installment for this plan
            row = conn.execute(
                """SELECT id FROM billing_installments
                   WHERE plan_id = ? AND status = 'scheduled'
                   ORDER BY installment_number LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if not row:
                return
            charged_at = now if status == "charged" else None
            conn.execute(
                """UPDATE billing_installments
                   SET status = ?, gateway_payment_id = ?, charged_at = ?
                   WHERE id = ?""",
                (status, gateway_payment_id, charged_at, row["id"]),
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
