"""
Tool: cancel_billing_plan
--------------------------
Cancel an active billing plan immediately.
Cancels the gateway subscription (Mollie/Stripe), marks all pending installments cancelled,
fires plan_cancelled external webhook.
No refund is issued.
"""
from __future__ import annotations

from typing import Callable, Dict

from core.namespace import validate_namespace
from engines.billing_engine.orchestrator import BillingOrchestrator


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def cancel_billing_plan(
        namespace: str,
        plan_id: str,
        reason: str = "",
    ) -> Dict:
        """
        Cancel a billing plan immediately. No refund is issued.

        Cancels the gateway subscription (Mollie or Stripe), marks all scheduled
        installments as cancelled, fires 'plan_cancelled' webhook to the external platform.

        plan_id: the plan ID returned by create_billing_plan.
        reason: optional cancellation reason (logged and forwarded in webhook).

        Returns: {success, plan_id, status}.
        """
        record_call("cancel_billing_plan", {"namespace": namespace, "plan_id": plan_id})

        try:
            namespace = validate_namespace(namespace)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not plan_id:
            return {"success": False, "error": "plan_id is required"}

        try:
            orchestrator = BillingOrchestrator(namespace=namespace)
            return orchestrator.cancel_plan(plan_id=plan_id, reason=reason)
        except Exception as exc:
            return {"success": False, "error": str(exc), "error_type": type(exc).__name__}
