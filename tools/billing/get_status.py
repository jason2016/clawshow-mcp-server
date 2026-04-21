"""
Tool: get_billing_status
-------------------------
Query the current status and installment schedule of a billing plan.
"""
from __future__ import annotations

from typing import Callable, Dict

from core.namespace import validate_namespace
from engines.billing_engine.orchestrator import BillingOrchestrator


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def get_billing_status(
        namespace: str,
        plan_id: str,
    ) -> Dict:
        """
        Get the current status of a billing plan: payment schedule, paid/pending/failed counts,
        next installment date, and gateway details.

        Args:
            namespace: Client namespace (e.g. "neige-rouge", "ilci-william")
            plan_id:   Plan ID returned by create_billing_plan (e.g. "plan_abc123def456")

        Returns:
            status, installments breakdown, next_installment, gateway_mode, customer info.
        """
        record_call("get_billing_status", {"namespace": namespace, "plan_id": plan_id})

        try:
            namespace = validate_namespace(namespace)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not plan_id or not plan_id.startswith("plan_"):
            return {"success": False, "error": "plan_id must start with 'plan_'"}

        try:
            orchestrator = BillingOrchestrator(namespace=namespace)
            return orchestrator.get_status(plan_id)
        except Exception as exc:
            return {"success": False, "error": str(exc), "error_type": type(exc).__name__}
