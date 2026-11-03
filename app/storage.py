"""Attachment storage in S3. The API never proxies file bytes: clients upload and download
directly with short-lived presigned URLs, and object keys never contain user-supplied names."""
from functools import lru_cache

import boto3
from botocore.config import Config

from app import config

UPLOAD_TTL = 900     # 15 minutes to start an upload
DOWNLOAD_TTL = 300   # 5 minutes per download link


@lru_cache(maxsize=1)
def s3():
    return boto3.client("s3", region_name=config.AWS_REGION,
                        config=Config(signature_version="s3v4", retries={"mode": "standard"}))


def object_key(team_id, note_id, attachment_id) -> str:
    return f"teams/{team_id}/notes/{note_id}/{attachment_id}"


def presign_upload(key: str, content_type: str, size_bytes: int) -> dict:
    """Presigned POST: S3 itself rejects a file of a different size or content type."""
    return s3().generate_presigned_post(
        config.ATTACHMENTS_BUCKET, key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", size_bytes, size_bytes]],
        ExpiresIn=UPLOAD_TTL)


def uploaded_size(key: str) -> int | None:
    try:
        return s3().head_object(Bucket=config.ATTACHMENTS_BUCKET, Key=key)["ContentLength"]
    except s3().exceptions.ClientError:
        return None


def presign_download(key: str, filename: str, content_type: str) -> str:
    safe = filename.replace('"', "").replace("\\", "")
    return s3().generate_presigned_url("get_object", ExpiresIn=DOWNLOAD_TTL, Params={
        "Bucket": config.ATTACHMENTS_BUCKET, "Key": key,
        # Always download, never render inline: an uploaded .html can't run in our origin.
        "ResponseContentDisposition": f'attachment; filename="{safe}"',
        "ResponseContentType": content_type})
