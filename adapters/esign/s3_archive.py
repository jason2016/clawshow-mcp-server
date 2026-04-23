"""eSign S3 archive — upload signed PDFs to AWS S3 (eu-west-3, GDPR).

Bucket: clawshow-esign-archive (eu-west-3, versioned, AES-256 encrypted, no public access)
Key:    signed/YYYY/MM/{doc_id}/{timestamp}_signed.pdf
TTL:    7-year legal retention tagged but not enforced via lifecycle (manual policy)
"""
import logging
import os
from datetime import datetime
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

S3_BUCKET = os.getenv("S3_ESIGN_BUCKET", "clawshow-esign-archive")
S3_REGION = os.getenv("AWS_REGION", "eu-west-3")


def _s3_client():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def upload_signed_pdf(
    doc_id: str,
    signer_email: str,
    pdf_bytes: bytes,
    doc_name: Optional[str] = None,
) -> dict:
    """Upload signed PDF to S3. Returns dict with s3_key, s3_url, size_bytes."""
    now = datetime.utcnow()
    key = f"signed/{now.year:04d}/{now.month:02d}/{doc_id}/{now.strftime('%Y%m%d_%H%M%S')}_signed.pdf"
    client = _s3_client()
    try:
        client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            ServerSideEncryption="AES256",
            Metadata={
                "doc-id": doc_id,
                "signer-email": signer_email[:50],
                "doc-name": (doc_name or "untitled")[:100],
                "uploaded-at": now.isoformat(),
            },
            Tagging="retention=7y&purpose=esign-archive",
        )
        s3_url = f"s3://{S3_BUCKET}/{key}"
        logger.info(
            "S3 archived: doc_id=%s signer=%s*** key=%s size=%d",
            doc_id, signer_email[:4], key, len(pdf_bytes),
        )
        return {"s3_key": key, "s3_url": s3_url, "size_bytes": len(pdf_bytes)}
    except ClientError as exc:
        logger.error(
            "S3 upload failed: doc_id=%s error=%s",
            doc_id,
            exc.response["Error"]["Code"],
        )
        raise


def generate_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generate a time-limited pre-signed download URL (default 1h)."""
    client = _s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=expires_in,
    )
