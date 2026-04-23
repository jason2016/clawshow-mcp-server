"""eSign OTP endpoints — Starlette-style async handlers.

Routes (registered in server.py):
  POST /esign/{document_id}/otp/send    — generate + email OTP to signer
  POST /esign/{document_id}/otp/verify  — verify OTP, mark signer otp_verified_at
"""
import logging
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

import db

logger = logging.getLogger(__name__)


async def esign_otp_send(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/otp/send

    Body: {"token": "<signer token from URL>"}
    """
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    if doc.get("status") in ("completed", "declined", "cancelled"):
        return JSONResponse({"error": f"Document already {doc['status']}"}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    token = body.get("token", "")
    signer = db.get_signer_by_token(token) if token else None
    if not signer or signer["document_id"] != doc_id:
        return JSONResponse({"error": "Invalid signer token"}, status_code=403)

    lock_info = db.is_otp_locked(doc_id, signer["id"])
    if lock_info["locked"]:
        return JSONResponse(
            {"error": "Too many failed attempts. Try again in 30 minutes."},
            status_code=429,
        )

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.create_otp(doc_id, signer["id"], code)

    try:
        from tools.esign import _send_otp_email
        _send_otp_email(signer["signer_name"], signer["signer_email"], code)
    except Exception as exc:
        logger.error("OTP email failed: doc_id=%s signer=%s err=%s", doc_id, signer["id"], exc)
        return JSONResponse({"error": "Failed to send OTP email"}, status_code=500)

    logger.info("OTP sent: doc_id=%s signer_id=%s", doc_id, signer["id"])
    db.log_esign_audit(doc_id, "otp_sent", {"signer_id": signer["id"]}, signer_id=signer["id"])
    return JSONResponse({"status": "sent", "expires_in_minutes": 10})


async def esign_otp_verify(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/otp/verify

    Body: {"token": "<signer token>", "otp": "123456"}
    """
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    token = body.get("token", "")
    otp_input = str(body.get("otp", "")).strip()

    signer = db.get_signer_by_token(token) if token else None
    if not signer or signer["document_id"] != doc_id:
        return JSONResponse({"error": "Invalid signer token"}, status_code=403)

    lock_info = db.is_otp_locked(doc_id, signer["id"])
    if lock_info["locked"]:
        return JSONResponse(
            {"error": "Too many failed attempts. Try again in 30 minutes."},
            status_code=429,
        )

    otp_record = db.get_active_otp(doc_id, signer["id"])
    if not otp_record:
        return JSONResponse({"error": "No active OTP. Please request a new code."}, status_code=400)

    if otp_input != otp_record["code"]:
        result = db.increment_otp_attempts(otp_record["id"], otp_record["attempts"])
        if result["locked"]:
            return JSONResponse(
                {"error": "Too many failed attempts. Account locked for 30 minutes."},
                status_code=429,
            )
        remaining = max(0, 5 - result["attempts"])
        return JSONResponse(
            {"error": f"Invalid code. {remaining} attempt(s) remaining."},
            status_code=400,
        )

    db.verify_otp(otp_record["id"], doc_id, signer["id"])
    logger.info("OTP verified: doc_id=%s signer_id=%s", doc_id, signer["id"])
    db.log_esign_audit(doc_id, "otp_verified", {"signer_id": signer["id"]}, signer_id=signer["id"])

    return JSONResponse({"verified": True})
