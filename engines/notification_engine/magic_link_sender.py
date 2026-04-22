"""
Magic Link email sender for billing payment pages.

Sends Resend emails with payment links for:
  - Initial payment (after create_billing_plan)
  - Retry (after failed charge)
  - Reminder (7 days before due)
  - Confirmation (after payment success)

From-address priority: namespace yaml > RESEND_FROM env var
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_PAY_BASE_URL = os.environ.get("PAYMENT_PAGE_BASE_URL", "https://clawshow.ai/pay/")

_MONTHS_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}


def _format_date_fr(date_str: str) -> str:
    try:
        d = datetime.fromisoformat(date_str[:10])
        return f"{d.day} {_MONTHS_FR[d.month]} {d.year}"
    except Exception:
        return date_str


def _format_amount(amount: float, currency: str) -> str:
    try:
        from babel.numbers import format_currency
        return format_currency(amount, currency, locale="fr_FR")
    except Exception:
        return f"{amount:,.2f} {currency}"


def _render_template(name: str, vars: dict) -> str:
    """Simple [[key]] substitution template rendering."""
    path = _TEMPLATE_DIR / name
    html = path.read_text(encoding="utf-8")
    for key, val in vars.items():
        html = html.replace(f"[[{key}]]", str(val) if val is not None else "")
    return html


class MagicLinkSender:
    """
    Sends magic link emails for billing payment pages.

    Usage:
        sender = MagicLinkSender()
        sender.send_initial(plan_id="plan_xxx", installment_no=1, namespace="neige-rouge")
    """

    def _get_from_email(self, ns_config) -> tuple[str, str]:
        """
        Returns (email, name).
        Priority: namespace yaml > env var default
        """
        email = (
            getattr(ns_config.notification, "email_from", None)
            or os.environ.get("RESEND_FROM", "noreply@clawshow.ai")
        )
        name = (
            getattr(ns_config.notification, "email_from_name", None)
            or os.environ.get("RESEND_FROM_NAME", "ClawShow")
        )
        return email, name

    def _send_email(
        self,
        to: str,
        from_email: str,
        from_name: str,
        subject: str,
        html: str,
    ) -> bool:
        import resend
        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            logger.error("RESEND_API_KEY not configured — email not sent to %s", to)
            return False

        resend.api_key = api_key
        try:
            params = resend.Emails.SendParams(
                from_=f"{from_name} <{from_email}>",
                to=[to],
                subject=subject,
                html=html,
            )
            result = resend.Emails.send(params)
            logger.info("Email sent: id=%s to=%s subject=%s", result.get("id"), to, subject)
            return True
        except Exception as exc:
            logger.error("Resend error: to=%s subject=%s error=%s", to, subject, exc)
            return False

    def send_initial(
        self,
        plan_id: str,
        installment_no: int,
        namespace: str,
        token: Optional[str] = None,
    ) -> bool:
        """
        Send initial payment magic link email.
        Called by orchestrator after create_billing_plan (non-contract plans).

        Args:
            plan_id: billing plan ID
            installment_no: which installment (1-based; 0 for subscription first charge)
            namespace: customer namespace
            token: pre-generated token (if None, fetches from DB)
        """
        from storage.billing_db import BillingDB
        from core.namespace_config import load_namespace_config
        from core.payment_token import get_token_for_installment

        db = BillingDB()
        plan = db.get_plan(plan_id, namespace)
        if not plan:
            logger.error("send_initial: plan not found %s/%s", namespace, plan_id)
            return False

        customer_email = plan.get("customer_email", "")
        if not customer_email:
            logger.error("send_initial: no customer_email on plan %s", plan_id)
            return False

        # Get token
        if not token:
            token = get_token_for_installment(plan_id, installment_no)
        if not token:
            logger.error("send_initial: no token for plan=%s inst=%d", plan_id, installment_no)
            return False

        payment_url = f"{_PAY_BASE_URL}{token}"
        ns_config = load_namespace_config(namespace)
        from_email, from_name = self._get_from_email(ns_config)

        # Build template variables
        total = plan.get("installments", 1)
        is_subscription = (total == -1)
        installments_list = db.get_installments(plan_id)
        target = next(
            (i for i in installments_list if i["installment_number"] == installment_no),
            installments_list[0] if installments_list else None,
        )

        # Use installment amount if available, otherwise fall back to plan total
        if target:
            amount = target.get("amount", 0)
        else:
            amount = plan.get("total_amount", 0)
        currency = plan.get("currency", "EUR")

        if is_subscription:
            intro_text = (
                f"Merci de votre confiance envers {ns_config.brand.name}. "
                "Pour activer votre abonnement mensuel, cliquez sur le bouton ci-dessous :"
            )
        else:
            intro_text = (
                "Votre plan de paiement a été créé. "
                "Cliquez sur le bouton ci-dessous pour régler la première échéance :"
            )

        expires_at_str = ""
        try:
            from core.payment_token import get_token_record
            rec = get_token_record(token)
            if rec and rec.get("expires_at"):
                expires_at_str = _format_date_fr(rec["expires_at"][:10])
        except Exception:
            expires_at_str = _format_date_fr(
                (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
            )

        # Build optional HTML blocks
        installment_block = ""
        if not is_subscription and total and total > 0:
            installment_block = (
                f'<p style="margin:0 0 4px 0;font-size:13px;color:#888888;">Échéance</p>'
                f'<p style="margin:0 0 12px 0;font-size:15px;color:#333333;">'
                f'{installment_no} sur {total}'
                f'</p>'
            )

        due_date_block = ""
        if target and target.get("scheduled_date"):
            due_date_block = (
                f'<p style="margin:0 0 4px 0;font-size:13px;color:#888888;">Date limite</p>'
                f'<p style="margin:0 0 12px 0;font-size:15px;color:#333333;">'
                f'{_format_date_fr(target["scheduled_date"])}'
                f'</p>'
            )

        description_block = ""
        desc = plan.get("description", "")
        if desc:
            description_block = (
                f'<p style="margin:0 0 4px 0;font-size:13px;color:#888888;">Détail</p>'
                f'<p style="margin:0;font-size:14px;color:#555555;">{desc}</p>'
            )

        support_email = ns_config.brand.support_email or ""
        support_block = ""
        if support_email:
            primary_color = ns_config.brand.primary_color or "#6366F1"
            support_block = (
                f'<p style="margin:16px 0 0 0;font-size:13px;color:#888888;text-align:center;">'
                f'Une question ? <a href="mailto:{support_email}" '
                f'style="color:{primary_color};text-decoration:none;">{support_email}</a>'
                f'</p>'
            )

        html = _render_template("magic_link_initial.html", {
            "brand_name": ns_config.brand.name or namespace,
            "primary_color": ns_config.brand.primary_color or "#6366F1",
            "customer_name": plan.get("customer_name", ""),
            "intro_text": intro_text,
            "amount_formatted": _format_amount(amount, currency),
            "installment_block": installment_block,
            "due_date_block": due_date_block,
            "description_block": description_block,
            "payment_url": payment_url,
            "expires_at": expires_at_str,
            "support_block": support_block,
        })

        subject = f"{ns_config.brand.name} – Votre lien de paiement"
        ok = self._send_email(
            to=customer_email,
            from_email=from_email,
            from_name=from_name,
            subject=subject,
            html=html,
        )

        logger.info(
            "send_initial: plan=%s inst=%d to=%s ok=%s",
            plan_id, installment_no, customer_email, ok,
        )
        return ok


# Module-level singleton
_sender = MagicLinkSender()


def send_magic_link_initial(
    plan_id: str,
    installment_no: int,
    namespace: str,
    token: Optional[str] = None,
) -> bool:
    """Convenience function — uses the module singleton."""
    return _sender.send_initial(
        plan_id=plan_id,
        installment_no=installment_no,
        namespace=namespace,
        token=token,
    )
