"""
BillingOrchestrator — Week 3 (2026-04-21)
Coordinates: DB → adapter (Mollie/Stripe) → schedule_calculator.

Week 1: create_plan (Mollie TEST)
Week 2: webhook handler, retry scheduler, outbound webhook
Week 3: Stripe adapter, eSign integration, cancel_plan
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


def get_gateway_mode(namespace: str, gateway: str = "mollie") -> str:
    from core.config import get_gateway_mode as _cfg_mode
    return _cfg_mode(namespace, gateway)


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

        # --- Create gateway customer (Mollie only; Stripe creates customer in subscription block) ---
        gateway_customer_id = ""
        gateway_mandate_id = None

        if gateway != "stripe":
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

            # Mollie: create mandate (test mode only)
            if mode == "test" and frequency != "one_time":
                try:
                    mdt = create_test_mandate(
                        customer_id=gateway_customer_id,
                        consumer_name=customer_name,
                    )
                    gateway_mandate_id = mdt["mandate_id"]
                except Exception as exc:
                    logger.warning("Test mandate creation failed (non-fatal): %s", exc)

        # --- Create subscription (skip if contract must be signed first) -------
        per_installment = installment_amount(total_amount, installments)

        gateway_plan_id = None
        initial_status = "active"

        # When contract is required, defer subscription creation until after signature
        skip_subscription = contract_required and bool(contract_pdf_url)

        if frequency == "one_time":
            initial_status = "pending_charge"
        elif skip_subscription:
            # Plan stays pending_signature; subscription created in esign_integration._activate_plan
            initial_status = "pending_signature"
        elif gateway == "stripe":
            try:
                from adapters.stripe.customer import create_stripe_customer
                from adapters.stripe.subscription import create_stripe_subscription
                cust_stripe = create_stripe_customer(
                    email=customer_email,
                    name=customer_name,
                    phone=customer_phone,
                    metadata=customer_metadata,
                    mode=mode,
                )
                gateway_customer_id = cust_stripe["customer_id"]
                sub = create_stripe_subscription(
                    customer_id=gateway_customer_id,
                    amount=per_installment,
                    currency=currency,
                    frequency=frequency,
                    start_date=start_date,
                    description=description or f"ClawShow {self.namespace}",
                    namespace=self.namespace,
                    plan_id=plan_id,
                    installments=installments,
                    mode=mode,
                )
                gateway_plan_id = sub["subscription_id"]
            except Exception as exc:
                logger.error("Stripe create_subscription failed: %s", exc)
                return {"success": False, "error": f"Gateway error creating subscription: {exc}"}
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
                    times=None if installments == -1 else installments,
                )
                gateway_plan_id = sub["subscription_id"]
            except Exception as exc:
                logger.error("Mollie create_subscription failed: %s", exc)
                return {"success": False, "error": f"Gateway error creating subscription: {exc}"}

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

        # --- Auto-generate payment tokens for all installments ---------------
        from core.payment_token import create_token_record
        payment_page_base = os.environ.get("PAYMENT_PAGE_BASE_URL", "https://clawshow.ai/pay/")
        first_token = None
        payment_url = None
        if installments == -1:
            # Infinite subscription: generate 1 token for the first charge
            first_token = create_token_record(
                plan_id=plan_id,
                installment_no=0,
                namespace=self.namespace,
                amount=per_installment,
                currency=currency,
            )
            payment_url = f"{payment_page_base}{first_token}"
        else:
            # Fixed installments: generate a token per installment
            for inst_row in installment_rows:
                inst_no = inst_row.get("installment_number", 1)
                tok = create_token_record(
                    plan_id=plan_id,
                    installment_no=inst_no,
                    namespace=self.namespace,
                    amount=inst_row.get("amount", per_installment),
                    currency=currency,
                )
                if inst_no == 1:
                    first_token = tok
            payment_url = f"{payment_page_base}{first_token}" if first_token else None

        # --- Auto-trigger eSign if contract required --------------------------
        esign_request_id = None
        signing_url = None
        if skip_subscription:
            esign_result = _send_esign_sync(
                plan_id=plan_id,
                namespace=self.namespace,
                contract_pdf_url=contract_pdf_url,
                signers=signers,
                customer_email=customer_email,
                customer_name=customer_name,
                description=description or f"Contract for plan {plan_id}",
            )
            if esign_result.get("ok"):
                esign_request_id = esign_result.get("esign_request_id")
                signing_url = esign_result.get("signing_url")
                if esign_request_id:
                    self.db.update_plan_status(plan_id, self.namespace, "pending_signature",
                                               contract_esign_request_id=esign_request_id)
            else:
                logger.warning("eSign trigger failed (non-fatal): %s", esign_result.get("error"))

        # --- Send magic link email (non-contract plans only) -----------------
        # Contract plans wait for eSign callback before sending the payment link
        if not skip_subscription and first_token and customer_email:
            try:
                from engines.notification_engine.magic_link_sender import send_magic_link_initial
                send_magic_link_initial(
                    plan_id=plan_id,
                    installment_no=1 if installments != -1 else 0,
                    namespace=self.namespace,
                    token=first_token,
                )
            except Exception as exc:
                logger.warning("Magic link email failed (non-fatal): %s", exc)

        # --- External webhook ------------------------------------------------
        if external_webhook_url:
            from engines.billing_engine.webhook_sender import send_external_webhook_sync
            send_external_webhook_sync(
                webhook_url=external_webhook_url,
                auth_token=external_auth_token or "",
                event_type="plan_created",
                plan_id=plan_id,
                order_id=external_order_id or "",
                namespace=self.namespace,
                payload={"status": initial_status, "total_amount": total_amount, "installments": installments},
            )

        commission_preview = round(total_amount * 0.005, 2)

        result = {
            "success": True,
            "plan_id": plan_id,
            "gateway_plan_id": gateway_plan_id,
            "gateway_customer_id": gateway_customer_id,
            "gateway_mode": mode,
            "status": initial_status,
            "schedule": schedule[:3],
            "total_installments": installments if installments == -1 else len(schedule),
            "per_installment_amount": per_installment,
            "currency": currency,
            "next_action": "wait_for_signature" if initial_status == "pending_signature" else "active",
            "commission_preview": commission_preview,
            "payment_url": payment_url,
        }
        if initial_status == "pending_signature":
            result["contract_signing"] = {
                "status": "pending_signature",
                "esign_request_id": esign_request_id,
                "signing_url": signing_url,
            }
        return result

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


    def cancel_plan(self, plan_id: str, reason: str = "") -> dict:
        plan = self.db.get_plan(plan_id, self.namespace)
        if not plan:
            return {"success": False, "error": f"Plan '{plan_id}' not found"}

        if plan["status"] in ("cancelled", "completed"):
            return {"success": False, "error": f"Plan is already {plan['status']}"}

        gateway = plan.get("gateway", "mollie")
        gateway_plan_id = plan.get("gateway_plan_id")
        mode = plan.get("gateway_mode", "test")

        # Cancel at gateway level if subscription exists
        if gateway_plan_id:
            if gateway == "mollie":
                try:
                    from adapters.mollie.subscription import cancel_mollie_subscription
                    cancel_mollie_subscription(
                        customer_id=plan["gateway_customer_id"],
                        subscription_id=gateway_plan_id,
                        mode=mode,
                    )
                except Exception as exc:
                    logger.warning("Mollie cancel_subscription failed (non-fatal): %s", exc)
            elif gateway == "stripe":
                try:
                    from adapters.stripe.subscription import cancel_stripe_subscription
                    cancel_stripe_subscription(subscription_id=gateway_plan_id, mode=mode)
                except Exception as exc:
                    logger.warning("Stripe cancel_subscription failed (non-fatal): %s", exc)

        self.db.cancel_pending_installments(plan_id)
        self.db.update_plan_status(plan_id, self.namespace, "cancelled")

        if plan.get("external_webhook_url"):
            from engines.billing_engine.webhook_sender import send_external_webhook_sync
            send_external_webhook_sync(
                webhook_url=plan["external_webhook_url"],
                auth_token=plan.get("external_auth_token") or "",
                event_type="plan_cancelled",
                plan_id=plan_id,
                order_id=plan.get("external_order_id") or "",
                namespace=self.namespace,
                payload={"reason": reason or "api_request"},
            )

        return {"success": True, "plan_id": plan_id, "status": "cancelled"}

    def activate_subscription_for_plan(self, plan_id: str) -> dict:
        """Called after eSign callback: flip plan from pending_signature to active."""
        plan = self.db.get_plan(plan_id, self.namespace)
        if not plan:
            return {"success": False, "error": f"Plan '{plan_id}' not found"}
        new_status = "pending_charge" if plan["frequency"] == "one_time" else "active"
        self.db.update_plan_status(plan_id, self.namespace, new_status)
        return {"success": True, "plan_id": plan_id, "new_status": new_status}


def _send_esign_sync(
    plan_id: str,
    namespace: str,
    contract_pdf_url: str,
    signers: list,
    customer_email: str,
    customer_name: str,
    description: str = "",
) -> dict:
    """
    Synchronous HTTP call to /esign/create.
    Used from create_plan (sync context).
    Falls back gracefully on error.
    """
    import os
    import requests

    base_url = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")

    signer_list = signers if signers else [{"name": customer_name, "email": customer_email, "role": "signer"}]

    payload = {
        "namespace": namespace,
        "document_url": contract_pdf_url,
        "signers": signer_list,
        "description": description,
        "callback_metadata": {"plan_id": plan_id, "namespace": namespace},
        "webhook_url": f"{base_url}/webhooks/esign",
    }
    try:
        resp = requests.post(f"{base_url}/esign/create", json=payload, timeout=15)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        esign_id = data.get("document_id") or data.get("id") or data.get("esign_request_id")
        return {
            "ok": bool(esign_id),
            "esign_request_id": esign_id,
            "signing_url": data.get("signing_url", ""),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _mollie_interval(frequency: str) -> str:
    return {"monthly": "1 month", "quarterly": "3 months", "weekly": "1 week"}.get(frequency, "1 month")


def _next_pending(installments: list[dict]) -> dict | None:
    for i in installments:
        if i["status"] == "scheduled":
            return {"installment_number": i["installment_number"], "scheduled_date": i["scheduled_date"], "amount": i["amount"]}
    return None
