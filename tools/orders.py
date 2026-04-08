"""
Tool: manage_orders
--------------------
Universal order management. Create orders with auto Stripe payment links,
query/filter/update orders, process refunds. Storage: JSON files per namespace.

Env required (for payment/refund features):
  STRIPE_SECRET_KEY — Stripe secret key
"""

from __future__ import annotations

import os
import json
import glob
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_DATA_ROOT = Path(__file__).parent.parent / "data" / "orders"


def _ns_dir(namespace: str) -> Path:
    d = _DATA_ROOT / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_order_id(namespace: str) -> str:
    """Generate ORD-YYYYMMDD-NNN, incrementing NNN within the same day."""
    today = date.today().strftime("%Y%m%d")
    prefix = f"ORD-{today}-"
    d = _ns_dir(namespace)
    existing = [f.stem for f in d.glob(f"{prefix}*.json")]
    if existing:
        nums = [int(name.split("-")[-1]) for name in existing]
        seq = max(nums) + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def _save(namespace: str, order: dict) -> None:
    path = _ns_dir(namespace) / f"{order['order_id']}.json"
    path.write_text(json.dumps(order, indent=2, ensure_ascii=False), encoding="utf-8")


def _load(namespace: str, order_id: str) -> dict | None:
    path = _ns_dir(namespace) / f"{order_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _load_all(namespace: str) -> list[dict]:
    d = _ns_dir(namespace)
    orders = []
    for f in sorted(d.glob("ORD-*.json")):
        try:
            orders.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return orders


def _mark_overdue(orders: list[dict]) -> list[dict]:
    """Auto-mark pending orders past due_date as overdue."""
    today_str = date.today().isoformat()
    for o in orders:
        if o.get("status") == "pending" and o.get("due_date") and o["due_date"] < today_str:
            o["status"] = "overdue"
            # Persist the change
            ns = o.get("namespace", "")
            if ns:
                _save(ns, o)
    return orders


# ---------------------------------------------------------------------------
# Stripe integration helpers (direct import, no MCP re-call)
# ---------------------------------------------------------------------------

def _create_stripe_session(amount: float, currency: str, description: str,
                           customer_email: str, order_id: str) -> dict:
    """Create Stripe Checkout Session. Returns {payment_url, session_id} or {error}."""
    import stripe
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret_key:
        return {"error": "STRIPE_SECRET_KEY not configured"}
    stripe.api_key = secret_key

    amount_cents = int(round(amount * 100))
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            success_url="https://clawshow.ai/payment-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://clawshow.ai/payment-cancelled",
            customer_email=customer_email or None,
            metadata={"order_id": order_id},
            line_items=[{
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": amount_cents,
                    "product_data": {"name": description},
                },
                "quantity": 1,
            }],
        )
        return {"payment_url": session.url, "session_id": session.id}
    except Exception as e:
        return {"error": str(e)}


def _create_stripe_refund(session_id: str, amount: float | None = None) -> dict:
    """Create Stripe refund. Returns {refund_id, amount} or {error}."""
    import stripe
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret_key:
        return {"error": "STRIPE_SECRET_KEY not configured"}
    stripe.api_key = secret_key

    try:
        # Get payment intent from session
        session = stripe.checkout.Session.retrieve(session_id)
        pi_id = session.payment_intent
        if not pi_id:
            return {"error": "No payment intent found for this session"}

        params: dict = {"payment_intent": pi_id}
        if amount is not None:
            params["amount"] = int(round(amount * 100))

        refund = stripe.Refund.create(**params)
        return {"refund_id": refund.id, "amount": refund.amount / 100}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Notification helper (direct import)
# ---------------------------------------------------------------------------

def _send_order_email(customer_email: str, order: dict) -> bool:
    """Send booking confirmation email. Returns True on success."""
    try:
        import resend
        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            return False
        resend.api_key = api_key

        payment_info = ""
        if order.get("payment_url"):
            payment_info = (
                f'<p style="margin:16px 0"><a href="{order["payment_url"]}" '
                f'style="background:#16a34a;color:#fff;padding:12px 24px;border-radius:8px;'
                f'text-decoration:none;font-weight:600">Pay Now</a></p>'
            )

        html = f"""
<div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:#fff">
  <h2 style="color:#111827;font-size:20px;margin:0 0 8px">Order Created</h2>
  <p style="color:#6b7280;font-size:14px;margin:0 0 24px">Here are the details of your order.</p>
  <div style="background:#f9fafb;border-radius:12px;padding:20px;margin-bottom:24px">
    <table style="width:100%;font-size:14px;color:#374151">
      <tr><td style="padding:6px 0;color:#9ca3af">Order</td><td style="padding:6px 0;text-align:right;font-weight:600">{order['order_id']}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Description</td><td style="padding:6px 0;text-align:right">{order['description']}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Amount</td><td style="padding:6px 0;text-align:right;font-weight:600">{order['currency'].upper()} {order['amount']:.2f}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Due</td><td style="padding:6px 0;text-align:right">{order.get('due_date', '-')}</td></tr>
    </table>
  </div>
  {payment_info}
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;text-align:center">
    <p style="color:#9ca3af;font-size:12px;margin:0">Powered by <a href="https://clawshow.ai" style="color:#9ca3af">ClawShow</a></p>
  </div>
</div>"""

        resend.Emails.send({
            "from": "ClawShow <onboarding@resend.dev>",
            "to": [customer_email],
            "subject": f"Order {order['order_id']} — {order['description']}",
            "html": html,
        })
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_create(namespace: str, params: dict) -> dict:
    now = datetime.now(timezone.utc)
    order_id = _next_order_id(namespace)

    order: dict = {
        "order_id":       order_id,
        "namespace":      namespace,
        "status":         "pending",
        "customer_name":  params.get("customer_name", ""),
        "customer_email": params.get("customer_email", ""),
        "amount":         params.get("amount", 0),
        "currency":       params.get("currency", "eur"),
        "description":    params.get("description", ""),
        "category":       params.get("category", "other"),
        "due_date":       params.get("due_date", ""),
        "metadata":       params.get("metadata", {}),
        "payment_url":    "",
        "session_id":     "",
        "notification_sent": False,
        "created_at":     now.isoformat(),
        "updates":        [],
    }

    # Auto payment link
    if params.get("auto_payment_link", True):
        result = _create_stripe_session(
            amount=order["amount"],
            currency=order["currency"],
            description=order["description"],
            customer_email=order["customer_email"],
            order_id=order_id,
        )
        if "payment_url" in result:
            order["payment_url"] = result["payment_url"]
            order["session_id"] = result["session_id"]

    _save(namespace, order)

    # Auto notify
    if params.get("auto_notify", False) and order["customer_email"]:
        order["notification_sent"] = _send_order_email(order["customer_email"], order)
        _save(namespace, order)

    return order


def _action_query(namespace: str, params: dict) -> dict:
    orders = _load_all(namespace)
    orders = _mark_overdue(orders)

    # Filters
    status = params.get("status")
    email = params.get("customer_email")
    category = params.get("category")
    period = params.get("period")
    limit = params.get("limit", 20)

    if status:
        orders = [o for o in orders if o.get("status") == status]
    if email:
        orders = [o for o in orders if o.get("customer_email") == email]
    if category:
        orders = [o for o in orders if o.get("category") == category]

    if period:
        today = date.today()
        if period == "today":
            prefix = today.isoformat()
            orders = [o for o in orders if o.get("created_at", "").startswith(prefix)]
        elif period == "week":
            week_start = (today - timedelta(days=today.weekday())).isoformat()
            orders = [o for o in orders if o.get("created_at", "")[:10] >= week_start]
        elif period == "month":
            month_prefix = today.strftime("%Y-%m")
            orders = [o for o in orders if o.get("created_at", "")[:7] == month_prefix]
        elif len(period) == 7:  # "2026-07"
            orders = [o for o in orders if o.get("created_at", "")[:7] == period]

    # Summary
    all_for_summary = orders
    summary = {
        "pending":           len([o for o in all_for_summary if o.get("status") == "pending"]),
        "paid":              len([o for o in all_for_summary if o.get("status") == "paid"]),
        "overdue":           len([o for o in all_for_summary if o.get("status") == "overdue"]),
        "completed":         len([o for o in all_for_summary if o.get("status") == "completed"]),
        "refunded":          len([o for o in all_for_summary if o.get("status") == "refunded"]),
        "total_amount":      sum(o.get("amount", 0) for o in all_for_summary),
        "total_paid":        sum(o.get("amount", 0) for o in all_for_summary if o.get("status") in ("paid", "completed")),
        "total_outstanding": sum(o.get("amount", 0) for o in all_for_summary if o.get("status") in ("pending", "overdue")),
    }

    # Apply limit
    orders = orders[-limit:]

    return {"total": len(orders), "orders": orders, "summary": summary}


def _action_update(namespace: str, params: dict) -> dict:
    order_id = params.get("order_id", "")
    new_status = params.get("status", "")
    note = params.get("note", "")

    order = _load(namespace, order_id)
    if not order:
        return {"status": "error", "message": f"Order {order_id} not found in namespace '{namespace}'"}

    old_status = order.get("status")
    order["status"] = new_status
    order.setdefault("updates", []).append({
        "from": old_status,
        "to": new_status,
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    _save(namespace, order)

    return {
        "order_id": order_id,
        "old_status": old_status,
        "new_status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _action_refund(namespace: str, params: dict) -> dict:
    order_id = params.get("order_id", "")
    reason = params.get("reason", "")
    refund_amount = params.get("amount")

    order = _load(namespace, order_id)
    if not order:
        return {"status": "error", "message": f"Order {order_id} not found in namespace '{namespace}'"}

    session_id = order.get("session_id")
    if not session_id:
        return {"status": "error", "message": "No Stripe session_id on this order — cannot refund"}

    result = _create_stripe_refund(session_id, refund_amount)
    if "error" in result:
        return {"status": "error", "message": result["error"]}

    order["status"] = "refunded"
    order.setdefault("updates", []).append({
        "from": order.get("status", "unknown"),
        "to": "refunded",
        "note": reason or "Refund processed",
        "refund_id": result["refund_id"],
        "refund_amount": result["amount"],
        "at": datetime.now(timezone.utc).isoformat(),
    })
    _save(namespace, order)

    return {
        "order_id": order_id,
        "refund_id": result["refund_id"],
        "refund_amount": result["amount"],
        "status": "refunded",
        "refunded_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Webhook helper — called from server.py stripe_webhook
# ---------------------------------------------------------------------------

def webhook_mark_paid(order_id: str) -> bool:
    """Find order by ID across all namespaces and mark as paid. Returns True if found."""
    for ns_dir in _DATA_ROOT.iterdir():
        if not ns_dir.is_dir():
            continue
        path = ns_dir / f"{order_id}.json"
        if path.exists():
            order = json.loads(path.read_text(encoding="utf-8"))
            if order.get("status") in ("pending", "overdue"):
                order["status"] = "paid"
                order.setdefault("updates", []).append({
                    "from": "pending",
                    "to": "paid",
                    "note": "Auto-marked by Stripe webhook",
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                path.write_text(json.dumps(order, indent=2, ensure_ascii=False), encoding="utf-8")
            return True
    return False


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def manage_orders(
        action: str,
        namespace: str,
        customer_name: str = "",
        customer_email: str = "",
        customer_phone: str = "",
        amount: float = 0,
        currency: str = "eur",
        description: str = "",
        category: str = "other",
        due_date: str = "",
        metadata: dict | None = None,
        auto_payment_link: bool = True,
        auto_notify: bool = False,
        status: str = "",
        period: str = "",
        limit: int = 20,
        order_id: str = "",
        booking_id: int = 0,
        booking_code: str = "",
        booking_date: str = "",
        booking_time: str = "",
        booking_type: str = "emporter",
        items: list | None = None,
        note: str = "",
        reason: str = "",
    ) -> str:
        """
        Create, query, update, and process refunds for orders. Works for any
        transaction-based business: restaurants, e-commerce, schools, service
        providers. Auto-creates orders from payment webhooks when integrated
        with generate_payment. Input: action (create/query/update/refund), order
        data, namespace. Output: order details with status, line items, payment
        info. Supports status tracking: pending → paid → processing → completed
        → refunded. Namespace-isolated for multi-tenant use.

        Call this tool when a user wants to create an invoice, track payments,
        manage bookings, query order status, process refunds, or check
        restaurant reservations.

        Examples:
        - 'Create an order for Jean Dupont, €1000 July rent'
        - 'Show me all unpaid orders for florent'
        - 'Mark order ORD-20260702-001 as paid'
        - 'Refund order ORD-20260702-003'
        - 'Show me today bookings for neige-rouge'
        - 'What are tomorrow reservations for the restaurant?'
        - 'Cancel booking #42'
        - 'Combien de commandes pour demain au restaurant?'
        - 'Check in booking 003'
        - '003到了' (checkin by code)

        Args:
            action:           "create" | "query" | "update" | "refund" |
                              "query_bookings" | "booking_summary" | "cancel_booking" | "checkin"
            namespace:        Business namespace, e.g. "florent", "neige-rouge"

            # create params:
            customer_name:    Customer full name
            customer_email:   Customer email
            customer_phone:   Customer phone
            amount:           Amount (e.g. 1000.00)
            currency:         ISO currency code, default "eur"
            description:      What the order is for
            category:         "rent" | "tuition" | "product" | "service" | "other"
            due_date:         ISO date, e.g. "2026-07-15"
            metadata:         Custom key-value pairs
            auto_payment_link: Auto-generate Stripe link (default true)
            auto_notify:      Auto-send email to customer (default false)

            # query params:
            status:           Filter: "pending" | "paid" | "completed" | "refunded" | "overdue"
            period:           "today" | "week" | "month" | "2026-07"
            limit:            Max results (default 20)

            # update params:
            order_id:         Order ID to update
            note:             Optional note

            # refund params:
            reason:           Refund reason

            # booking params:
            booking_code:     3-digit code for checkin (e.g. "011")
            booking_id:       Booking ID (for cancel_booking)
            booking_date:     Date filter for query_bookings / booking_summary (YYYY-MM-DD)
            booking_time:     Time for booking
            booking_type:     "surPlace" | "emporter"
            items:            List of items [{id, name, qty, price}]

        Returns:
            JSON with order/booking details, query results, or confirmation.
        """
        record_call("manage_orders")

        import db

        params = {
            "customer_name": customer_name, "customer_email": customer_email,
            "customer_phone": customer_phone,
            "amount": amount, "currency": currency, "description": description,
            "category": category, "due_date": due_date, "metadata": metadata or {},
            "auto_payment_link": auto_payment_link, "auto_notify": auto_notify,
            "status": status, "period": period, "limit": limit,
            "order_id": order_id, "note": note, "reason": reason,
        }

        if action == "create":
            result = _action_create(namespace, params)
        elif action in ("query", "query_bookings"):
            # Query both JSON orders AND SQLite bookings, return combined
            json_result = _action_query(namespace, params)
            bookings = db.query_bookings(namespace, date=booking_date, status=status, limit=limit)
            if bookings or action == "query_bookings":
                json_result["bookings"] = bookings
                json_result["total_bookings"] = len(bookings)
            result = json_result
        elif action == "update":
            result = _action_update(namespace, params)
        elif action == "refund":
            result = _action_refund(namespace, params)
        elif action == "booking_summary":
            date = booking_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            result = db.booking_summary(namespace, date)
        elif action == "cancel_booking":
            if not booking_id:
                result = {"status": "error", "message": "booking_id is required for cancel_booking"}
            else:
                result = db.cancel_booking(namespace, booking_id)
        elif action == "checkin":
            code = booking_code or order_id or (f"{booking_id:03d}" if booking_id else "")
            if not code:
                result = {"status": "error", "message": "booking_code is required for checkin (e.g. '011')"}
            else:
                # Ensure 3-digit zero-padded format
                code = code.zfill(3) if code.isdigit() else code
                result = db.checkin_by_code(namespace, code, booking_date)
        else:
            result = {"status": "error", "message": f"Unknown action: {action}. Use create/query/update/refund/query_bookings/booking_summary/cancel_booking/checkin."}

        return json.dumps(result, indent=2, ensure_ascii=False)
