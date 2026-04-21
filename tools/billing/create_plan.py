"""
Tool: create_billing_plan
--------------------------
Create a recurring or installment payment plan for a customer.
Supports: monthly/quarterly/weekly subscriptions, 1-N installments.

Week 1 (2026-04-21): Mollie TEST mode. No eSign. No scheduler.
Week 3: eSign + Stripe.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Dict

from core.namespace import validate_namespace
from engines.billing_engine.orchestrator import BillingOrchestrator


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def create_billing_plan(
        namespace: str,
        customer_email: str,
        customer_name: str,
        total_amount: float,
        currency: str = "EUR",
        installments: int = 1,
        frequency: str = "monthly",
        start_date: str = "",
        gateway: str = "mollie",
        contract_pdf_url: str = "",
        contract_required_before_charge: bool = False,
        signers: list | None = None,
        contract_template: str = "",
        contract_variables: dict | None = None,
        external_platform_name: str = "",
        external_webhook_url: str = "",
        external_order_id: str = "",
        external_auth_token: str = "",
        description: str = "",
        customer_phone: str = "",
        customer_metadata: dict | None = None,
        notify_customer_email: bool = True,
        notify_customer_sms: bool = False,
        retry_on_failure: bool = True,
        max_retries: int = 3,
    ) -> Dict:
        """
        Create a billing plan for recurring or installment payments.

        Supports monthly/quarterly/weekly subscriptions and fixed installments.
        Phase 1: Mollie TEST gateway only. eSign via contract_pdf_url (Week 3).

        installments: number of payments. Use -1 for infinite recurring subscription.
        frequency: "monthly" | "quarterly" | "weekly" | "one_time"
        gateway: "mollie" (Phase 1) or "stripe" (Week 3)

        Contract Option A (Phase 1): provide contract_pdf_url — ClawShow sends for eSign (Week 3).
        Contract Option B (Phase 2): use contract_template — ClawShow generates PDF from template.

        External sync: if external_webhook_url provided, ClawShow fires standardized events to that URL.

        Returns plan_id, status, schedule preview, commission_preview.
        """
        record_call("create_billing_plan", {"namespace": namespace, "gateway": gateway})

        try:
            namespace = validate_namespace(namespace)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not customer_email or "@" not in customer_email:
            return {"success": False, "error": "Invalid customer_email"}

        if total_amount <= 0:
            return {"success": False, "error": "total_amount must be > 0"}

        if installments < 1 and installments != -1:
            return {"success": False, "error": "installments must be >= 1 or -1 for infinite subscription"}

        if gateway not in ("mollie", "stripe"):
            return {"success": False, "error": f"Unknown gateway: {gateway}. Use 'mollie' or 'stripe'"}

        if frequency not in ("monthly", "quarterly", "weekly", "one_time"):
            return {"success": False, "error": "frequency must be: monthly | quarterly | weekly | one_time"}

        if contract_template:
            return {
                "success": False,
                "error": "contract_template is a Phase 2 feature. Use contract_pdf_url for Phase 1.",
            }

        resolved_start = start_date or date.today().isoformat()

        try:
            orchestrator = BillingOrchestrator(namespace=namespace)
            return orchestrator.create_plan(
                customer_email=customer_email,
                customer_name=customer_name,
                customer_phone=customer_phone,
                total_amount=total_amount,
                currency=currency,
                installments=installments,
                frequency=frequency,
                start_date=resolved_start,
                gateway=gateway,
                contract_pdf_url=contract_pdf_url,
                contract_required=contract_required_before_charge,
                signers=signers or [],
                contract_template=contract_template,
                contract_variables=contract_variables or {},
                external_platform_name=external_platform_name,
                external_webhook_url=external_webhook_url,
                external_order_id=external_order_id,
                external_auth_token=external_auth_token,
                description=description,
                customer_metadata=customer_metadata or {},
                notify_email=notify_customer_email,
                notify_sms=notify_customer_sms,
                retry_on_failure=retry_on_failure,
                max_retries=max_retries,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc), "error_type": type(exc).__name__}
