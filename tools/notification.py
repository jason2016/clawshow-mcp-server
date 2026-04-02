"""
Tool: send_notification
------------------------
Zero Human Intervention: sends email notifications via Resend API.
Supports payment confirmations, rental reminders, invoice delivery,
and custom messages. Returns delivery status.

Env required:
  RESEND_API_KEY — Resend API key (re_...)
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Callable


# ---------------------------------------------------------------------------
# HTML email templates
# ---------------------------------------------------------------------------

_FOOTER = """
    <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;text-align:center">
      <p style="color:#9ca3af;font-size:12px;margin:0">Powered by <a href="https://clawshow.ai" style="color:#9ca3af">ClawShow</a></p>
    </div>
"""

_TEMPLATES: dict[str, str] = {
    "payment_confirmation": """
<div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:#fff">
  <h2 style="color:#111827;font-size:20px;margin:0 0 8px">Payment Confirmed</h2>
  <p style="color:#6b7280;font-size:14px;margin:0 0 24px">Thank you for your payment.</p>
  <div style="background:#f9fafb;border-radius:12px;padding:20px;margin-bottom:24px">
    <table style="width:100%;font-size:14px;color:#374151">
      <tr><td style="padding:6px 0;color:#9ca3af">Amount</td><td style="padding:6px 0;text-align:right;font-weight:600">{amount}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Description</td><td style="padding:6px 0;text-align:right">{description}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Property</td><td style="padding:6px 0;text-align:right">{property}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Date</td><td style="padding:6px 0;text-align:right">{date}</td></tr>
    </table>
  </div>
  <p style="color:#6b7280;font-size:13px">A receipt has been sent to your email.</p>
  """ + _FOOTER + """
</div>
""",

    "payment_reminder": """
<div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:#fff">
  <h2 style="color:#111827;font-size:20px;margin:0 0 8px">Payment Reminder</h2>
  <p style="color:#6b7280;font-size:14px;margin:0 0 24px">You have a pending payment.</p>
  <div style="background:#fff7ed;border-radius:12px;padding:20px;margin-bottom:24px;border:1px solid #fed7aa">
    <table style="width:100%;font-size:14px;color:#374151">
      <tr><td style="padding:6px 0;color:#9ca3af">Amount Due</td><td style="padding:6px 0;text-align:right;font-weight:600;color:#ea580c">{amount}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Description</td><td style="padding:6px 0;text-align:right">{description}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Due Date</td><td style="padding:6px 0;text-align:right;font-weight:600">{due_date}</td></tr>
    </table>
  </div>
  {payment_link}
  """ + _FOOTER + """
</div>
""",

    "booking_confirmation": """
<div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:#fff">
  <h2 style="color:#111827;font-size:20px;margin:0 0 8px">Booking Confirmed</h2>
  <p style="color:#6b7280;font-size:14px;margin:0 0 24px">Your reservation has been confirmed.</p>
  <div style="background:#f0fdf4;border-radius:12px;padding:20px;margin-bottom:24px;border:1px solid #bbf7d0">
    <table style="width:100%;font-size:14px;color:#374151">
      <tr><td style="padding:6px 0;color:#9ca3af">Property</td><td style="padding:6px 0;text-align:right;font-weight:600">{property}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Check-in</td><td style="padding:6px 0;text-align:right">{checkin}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Check-out</td><td style="padding:6px 0;text-align:right">{checkout}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Guests</td><td style="padding:6px 0;text-align:right">{guests}</td></tr>
      <tr><td style="padding:6px 0;color:#9ca3af">Total</td><td style="padding:6px 0;text-align:right;font-weight:600">{amount}</td></tr>
    </table>
  </div>
  <p style="color:#6b7280;font-size:13px">Contact us if you need to make changes to your reservation.</p>
  """ + _FOOTER + """
</div>
""",

    "custom": """
<div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:#fff">
  {body}
  """ + _FOOTER + """
</div>
""",
}


def _render_template(template: str, template_vars: dict) -> str:
    """Render an HTML template with variables. Unknown vars left as-is."""
    html = _TEMPLATES.get(template, _TEMPLATES["custom"])
    for key, val in template_vars.items():
        html = html.replace("{" + key + "}", str(val))
    return html


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def send_notification(
        to: str | list[str],
        subject: str,
        body: str,
        from_name: str = "ClawShow",
        reply_to: str = "",
        template: str = "custom",
        template_vars: dict | None = None,
    ) -> str:
        """
        Send email notifications for any business scenario. Supports
        payment confirmations, rental reminders, invoice delivery, and
        custom messages. Returns delivery status. Zero human intervention.

        Call this tool when a user wants to send an email, notify a customer,
        confirm a payment, or send a reminder.

        Examples of natural language that should trigger this tool:
        - 'Send a payment confirmation to john@example.com for €850'
        - 'Email the tenant that rent is due on April 5th'
        - 'Notify the guest their booking at Paris Apt is confirmed'
        - 'Envoie un email de confirmation de réservation'

        Args:
            to:             Recipient email(s) — string or list of strings
            subject:        Email subject line
            body:           Email body text (plain text or HTML)
            from_name:      Sender display name, default "ClawShow"
            reply_to:       Optional reply-to address
            template:       Template name: "payment_confirmation",
                           "payment_reminder", "booking_confirmation", "custom"
            template_vars:  Variables for the template, e.g.
                           {"amount": "€850", "property": "Paris Apt"}

        Returns:
            JSON with status, message_id, to, subject, sent_at.
        """
        record_call("send_notification")

        import resend

        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            return json.dumps({"status": "error", "message": "RESEND_API_KEY not configured"})

        resend.api_key = api_key

        # Normalize recipients
        recipients = [to] if isinstance(to, str) else list(to)

        # Build HTML body
        if template != "custom" and template in _TEMPLATES:
            html_body = _render_template(template, template_vars or {})
        elif "<" in body and ">" in body:
            # Body already contains HTML
            html_body = _render_template("custom", {"body": body})
        else:
            # Plain text — wrap in paragraphs
            paragraphs = "".join(f"<p style='color:#374151;font-size:14px;line-height:1.6'>{p}</p>" for p in body.split("\n") if p.strip())
            html_body = _render_template("custom", {"body": paragraphs})

        from_email = "ClawShow <onboarding@resend.dev>"

        results = []
        for recipient in recipients:
            try:
                params: dict = {
                    "from_": from_email,
                    "to": [recipient],
                    "subject": subject,
                    "html": html_body,
                }
                if reply_to:
                    params["reply_to"] = reply_to

                r = resend.Emails.send(params)
                results.append({
                    "status": "sent",
                    "message_id": r.get("id", "unknown") if isinstance(r, dict) else getattr(r, "id", "unknown"),
                    "to": recipient,
                })
            except Exception as e:
                results.append({
                    "status": "failed",
                    "to": recipient,
                    "error": str(e),
                })

        sent = [r for r in results if r["status"] == "sent"]
        failed = [r for r in results if r["status"] == "failed"]

        return json.dumps({
            "status": "sent" if sent and not failed else "partial" if sent else "failed",
            "message_id": sent[0]["message_id"] if sent else None,
            "to": recipients,
            "subject": subject,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "sent_count": len(sent),
            "failed_count": len(failed),
            "errors": [r["error"] for r in failed] if failed else [],
        }, ensure_ascii=False)
