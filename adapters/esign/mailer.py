"""eSign email transport — Resend API.

All eSign emails route through here. Replaces the Gmail SMTP path in tools/esign.py.
From address: ClawShow eSign <esign@clawshow.ai> (clawshow.ai verified in Resend dashboard).
"""
import os
import logging

import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "ClawShow eSign <esign@clawshow.ai>"


class ResendMailError(Exception):
    pass


def send_html(to: str, subject: str, html: str, from_address: str = FROM_ADDRESS) -> dict:
    """Send HTML email via Resend. Returns Resend response dict with 'id'."""
    if os.getenv("DEV_MODE", "false") == "true":
        logger.info("[DEV MODE] eSign email to=%s subject=%s (not sent)", to, subject)
        print(f"\n{'='*50}\n[DEV MODE] eSign email to={to}\nsubject: {subject}\n{'='*50}\n", flush=True)
        return {"id": "dev-mode"}

    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise ResendMailError("RESEND_API_KEY not configured")

    try:
        resp = requests.post(
            RESEND_API_URL,
            json={"from": from_address, "to": [to], "subject": subject, "html": html},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info(
            "Resend sent: to=%s*** subject=%.40s id=%s",
            to[:4],
            subject,
            str(result.get("id", "?"))[:12],
        )
        return result
    except requests.HTTPError as exc:
        logger.error(
            "Resend HTTP error %s: %s",
            exc.response.status_code,
            exc.response.text[:200],
        )
        raise ResendMailError(f"Resend returned {exc.response.status_code}") from exc
    except requests.RequestException as exc:
        logger.error("Resend request failed: %s", exc)
        raise ResendMailError(str(exc)) from exc
