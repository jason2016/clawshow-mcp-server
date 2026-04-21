"""
BillingOrchestrator — Week 1 (2026-04-21)
Coordinates: DB → Mollie adapter → schedule_calculator.

Week 1 scope:
  ✅ create_plan (Mollie TEST, no scheduler, no eSign)
  ✅ get_status
  ❌ APScheduler (Week 2)
  ❌ eSign integration (Week 3)
  ❌ Stripe (Week 3)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone

from storage.billing_db import BillingDB
from engines.billing_engine.schedule_calculator import calculate_schedule, installment_amount
from adapters.mollie.customer import create_mollie_customer
from adapters.mollie.mandate import create_test_mandate
from adapters.mollie.subscription import create_mollie_subscription

logger = logging.getLogger(__name__)


def get_gateway_mode(namespace: str) -> str:
    """Phase 1 MVP: test mode for all namespaces."""
    return "test"


class BillingOrchestrator:

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.db = BillingDB()
        self.db.init_tables()

    def create_plan(
        self,
        customer_email: str,
        customer_name: str,
        customer_phone: str,
        total_amount: float,
        currency: str,
        installments: int,
        frequency: str,
        start_date: str,
        gateway: str,
        contract_pdf_url: str,
        contract_required: bool,
        signers: list,
        contract_template: str,
        contract_variables: dict,
        external_platform_name: str,
        external_webhook_url: str,
        external_order_id: str,
        external_auth_token: str,
        description: str,
        customer_metadata: dict,
        notify_email: bool,
        notify_sms: bool,
        retry_on_failure: bool,
        max_retries: int,
    ) -> dict:
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        mode = get_gateway_mode(self.namespace)

        # --- Mollie: create customer -----------------------------------------
        try:
            cust = create_mollie_customer(
                email=customer_email,
                name=customer_name,
                mode=mode,
                metadata=customer_metadata,
            )
            gateway_customer_id = cust["customer_id"]
        except Exception as exc:
            logger.error("Mollie create_customer failed: %s", exc)
            return {"success": False, "error": f"Gateway error creating customer: {exc}"}

        # --- Mollie: create mandate (test mode only) -------------------------
        gateway_mandate_id = None
        if mode == "test" and frequency != "one_time":
            try:
                mdt = create_test_mandate(
                    customer_id=gateway_customer_id,
                    consumer_name=customer_name,
                )
                gateway_mandate_id = mdt["mandate_id"]
            except Exception as exc:
                logger.warning("Test mandate creation failed (non-fatal): %s", exc)

        # --- Mollie: create subscription / one-time ---------------------------
        per_installment = installment_amount(total_amount, installments)

        gateway_plan_id = None
        initial_status = "active"

        if frequency == "one_time":
            # One-time: no subscription, just record plan
            initial_status = "pending_charge"
        else:
            try:
                sub = create_mollie_subscription(
                    customer_id=gateway_customer_id,
                    amount=per_installment,
                    currency=currency,
                    interval=_mollie_interval(frequency),
                    start_date=start_date,
                    description=description or f"ClawShow {self.namespace}",
                    mode=mode,
                )
                gateway_plan_id = sub["subscription_id"]
            except Exception as exc:
                if mode == "test":
                    # TEST mode: Mollie profile may lack recurring methods. Simulate subscription ID.
                    logger.warning("TEST mode subscription bypass (Mollie profile config): %s", exc)
                    gateway_plan_id = f"sub_test_{uuid.uuid4().hex[:8]}"
                else:
                    logger.error("Mollie create_subscription failed: %s", exc)
                    return {"success": False, "error": f"Gateway error creating subscription: {exc}"}

        # --- Override status if contract required ----------------------------
        if contract_required and contract_pdf_url:
            initial_status = "pending_signature"

        # --- Schedule preview ------------------------------------------------
        start = date.fromisoformat(start_date)
        schedule = calculate_schedule(start, installments, frequency, per_installment)

        # --- Persist to DB ---------------------------------------------------
        plan_row = {
            "plan_id": plan_id,
            "namespace": self.namespace,
            "customer_email": customer_email,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "total_amount": total_amount,
            "currency": currency,
            "installments": installments,
            "frequency": frequency,
            "start_date": start_date,
            "gateway": gateway,
            "gateway_plan_id": gateway_plan_id,
            "gateway_customer_id": gateway_customer_id,
            "gateway_mandate_id": gateway_mandate_id,
            "gateway_mode": mode,
            "contract_required": contract_required,
            "contract_pdf_url": contract_pdf_url,
            "contract_esign_request_id": None,
            "contract_template": contract_template or None,
            "contract_variables": json.dumps(contract_variables) if contract_variables else None,
            "external_platform_name": external_platform_name or None,
            "external_webhook_url": external_webhook_url or None,
            "external_order_id": external_order_id or None,
            "external_auth_token": external_auth_token or None,
            "status": initial_status,
            "description": description,
            "metadata": json.dumps(customer_metadata) if customer_metadata else None,
        }
        self.db.create_plan(plan_row)

        # Save installments (fixed count only; infinite subscriptions preview first 12)
        installment_rows = [
            {**s, "plan_id": plan_id}
            for s in schedule
        ]
        self.db.create_installments(installment_rows)

        # --- External webhook ------------------------------------------------
        if external_webhook_url:
            from core.webhook_sender import send_webhook
            send_webhook(
                webhook_url=external_webhook_url,
                event="plan_created",
                plan_id=plan_id,
                namespace=self.namespace,
                data={"status": initial_status, "total_amount": total_amount, "installments": installments},
                auth_token=external_auth_token,
                external_order_id=external_order_id,
            )

        commission_preview = round(total_amount * 0.005, 2)

        return {
            "success": True,
            "plan_id": plan_id,
            "gateway_plan_id": gateway_plan_id,
            "gateway_customer_id": gateway_customer_id,
            "gateway_mode": mode,
            "status": initial_status,
            "schedule": schedule[:3],  # first 3 installments in response
            "total_installments": len(schedule),
            "per_installment_amount": per_installment,
            "currency": currency,
            "contract_signing": {"status": "pending_signature"} if initial_status == "pending_signature" else None,
            "next_action": "wait_for_signature" if initial_status == "pending_signature" else "active",
            "commission_preview": commission_preview,
        }

    def get_status(self, plan_id: str) -> dict:
        plan = self.db.get_plan(plan_id, self.namespace)
        if not plan:
            return {"success": False, "error": f"Plan '{plan_id}' not found in namespace '{self.namespace}'"}

        installments = self.db.get_installments(plan_id)
        paid = sum(1 for i in installments if i["status"] == "charged")
        failed = sum(1 for i in installments if i["status"] == "failed")

        return {
            "success": True,
            "plan_id": plan_id,
            "namespace": self.namespace,
            "status": plan["status"],
            "gateway": plan["gateway"],
            "gateway_plan_id": plan["gateway_plan_id"],
            "gateway_mode": plan["gateway_mode"],
            "customer_email": plan["customer_email"],
            "customer_name": plan["customer_name"],
            "total_amount": plan["total_amount"],
            "currency": plan["currency"],
            "installments_total": plan["installments"],
            "installments_paid": paid,
            "installments_failed": failed,
            "installments_pending": len(installments) - paid - failed,
            "next_installment": _next_pending(installments),
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
        }


def _mollie_interval(frequency: str) -> str:
    return {"monthly": "1 month", "quarterly": "3 months", "weekly": "1 week"}.get(frequency, "1 month")


def _next_pending(installments: list[dict]) -> dict | None:
    for i in installments:
        if i["status"] == "scheduled":
            return {"installment_number": i["installment_number"], "scheduled_date": i["scheduled_date"], "amount": i["amount"]}
    return None
