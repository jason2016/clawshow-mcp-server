"""
Subscription tier config + quota enforcement.

Tiers:
  free      → 5 envelopes/mo,  hard block at quota,  €0
  starter   → 30 envelopes/mo, €0.50 overage/env,    €9/mo
  pro       → 150 envelopes/mo, €0.35 overage/env,   €29/mo  (trial default)
  business  → unlimited,        €0/env,               €99/mo
  enterprise→ unlimited (quota=-1), custom pricing

Quota -1 = unlimited.
"""
import os
import sqlite3
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse

from tools.auth import _get_db, require_session

TIER_CONFIG: dict[str, dict] = {
    "free": {
        "label": "Free",
        "price_cents": 0,
        "envelope_quota": 5,
        "overage_rate_cents": 0,
        "hard_block": True,
    },
    "starter": {
        "label": "Starter",
        "price_cents": 900,
        "envelope_quota": 30,
        "overage_rate_cents": 50,
        "hard_block": False,
    },
    "pro": {
        "label": "Pro",
        "price_cents": 2900,
        "envelope_quota": 150,
        "overage_rate_cents": 35,
        "hard_block": False,
    },
    "business": {
        "label": "Business",
        "price_cents": 9900,
        "envelope_quota": -1,
        "overage_rate_cents": 0,
        "hard_block": False,
    },
    "enterprise": {
        "label": "Enterprise",
        "price_cents": -1,
        "envelope_quota": -1,
        "overage_rate_cents": 0,
        "hard_block": False,
    },
}


@dataclass
class QuotaCheckResult:
    allowed: bool
    is_overage: bool
    overage_rate_cents: int
    envelopes_used: int
    envelope_quota: int
    tier: str
    reason: str | None = None


def check_quota(namespace: str, cursor: sqlite3.Cursor) -> QuotaCheckResult:
    cursor.execute(
        """
        SELECT tier, envelope_quota, envelopes_used_this_period,
               overage_rate_cents, status, trial_ends_at
        FROM namespaces WHERE namespace = ?
        """,
        (namespace,),
    )
    row = cursor.fetchone()
    if not row:
        return QuotaCheckResult(
            allowed=False, is_overage=False, overage_rate_cents=0,
            envelopes_used=0, envelope_quota=0, tier="free",
            reason=f"Namespace '{namespace}' not found",
        )

    tier = row["tier"] or "free"
    quota = row["envelope_quota"] if row["envelope_quota"] is not None else 5
    used = row["envelopes_used_this_period"] or 0
    status = row["status"] or "trial"

    if status == "cancelled":
        return QuotaCheckResult(
            allowed=False, is_overage=False, overage_rate_cents=0,
            envelopes_used=used, envelope_quota=quota, tier=tier,
            reason="Account cancelled",
        )

    # Unlimited
    if quota == -1:
        return QuotaCheckResult(
            allowed=True, is_overage=False, overage_rate_cents=0,
            envelopes_used=used, envelope_quota=-1, tier=tier,
        )

    config = TIER_CONFIG.get(tier, TIER_CONFIG["free"])
    overage_rate = config["overage_rate_cents"]
    hard_block = config["hard_block"]

    if used < quota:
        return QuotaCheckResult(
            allowed=True, is_overage=False, overage_rate_cents=0,
            envelopes_used=used, envelope_quota=quota, tier=tier,
        )

    if hard_block:
        return QuotaCheckResult(
            allowed=False, is_overage=False, overage_rate_cents=0,
            envelopes_used=used, envelope_quota=quota, tier=tier,
            reason=f"Free plan limit reached ({quota} envelopes/month). Upgrade to continue.",
        )

    return QuotaCheckResult(
        allowed=True, is_overage=True, overage_rate_cents=overage_rate,
        envelopes_used=used, envelope_quota=quota, tier=tier,
    )


def increment_usage(namespace: str, document_id: str, cursor: sqlite3.Cursor,
                    is_overage: bool = False, overage_rate_cents: int = 0) -> None:
    import secrets as _secrets
    event_id = _secrets.token_urlsafe(12)
    cursor.execute(
        """
        INSERT INTO usage_events (id, namespace, esign_document_id, event_type,
                                  is_overage, overage_amount_cents)
        VALUES (?, ?, ?, 'envelope_sent', ?, ?)
        """,
        (event_id, namespace, document_id, int(is_overage), overage_rate_cents),
    )
    cursor.execute(
        """
        UPDATE namespaces
        SET envelopes_used_this_period = envelopes_used_this_period + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE namespace = ?
        """,
        (namespace,),
    )


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

async def subscriptions_current(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    ns = request.path_params.get("namespace") or request.query_params.get("namespace", "")

    conn = _get_db()
    try:
        cursor = conn.cursor()

        # Default to first namespace of user if not specified
        if not ns:
            cursor.execute(
                "SELECT namespace FROM user_namespaces WHERE user_id = ? LIMIT 1",
                (user["id"],),
            )
            row = cursor.fetchone()
            if not row:
                return JSONResponse({"error": "No namespace found"}, status_code=404)
            ns = row["namespace"]

        # Verify membership
        cursor.execute(
            "SELECT 1 FROM user_namespaces WHERE user_id = ? AND namespace = ?",
            (user["id"], ns),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Not found"}, status_code=404)

        cursor.execute("SELECT * FROM namespaces WHERE namespace = ?", (ns,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"error": "Namespace not found"}, status_code=404)

        tier = row["tier"] or "free"
        config = TIER_CONFIG.get(tier, TIER_CONFIG["free"])
        quota = row["envelope_quota"] if row["envelope_quota"] is not None else 5
        used = row["envelopes_used_this_period"] or 0

        return JSONResponse({
            "namespace": ns,
            "tier": tier,
            "tier_label": config["label"],
            "status": row["status"],
            "envelope_quota": quota,
            "quota_unlimited": quota == -1,
            "envelopes_used_this_period": used,
            "envelopes_remaining": max(0, quota - used) if quota != -1 else None,
            "trial_ends_at": row["trial_ends_at"],
            "price_lock_until": row["price_lock_until"],
            "is_founding_customer": bool(row["is_founding_customer"]),
            "upgrade_options": [
                k for k, v in TIER_CONFIG.items()
                if k != tier and v["price_cents"] >= 0
            ],
        })
    finally:
        conn.close()


async def subscriptions_upgrade_intent(request: Request) -> JSONResponse:
    """Stub: returns Stancer payment intent URL. Full impl in Day 4."""
    user, err = require_session(request)
    if err:
        return err

    return JSONResponse({
        "status": "coming_soon",
        "message": "Stancer billing integration coming in Day 4.",
    })


subscriptions_routes_list = [
    ("GET",  "/subscriptions/current",        subscriptions_current),
    ("POST", "/subscriptions/upgrade-intent", subscriptions_upgrade_intent),
]
