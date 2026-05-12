"""
S3 helpers — download a prefix into a local directory, upload a local directory to a prefix.
"""
import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from . import config

logger = logging.getLogger(__name__)


def _client():
    return boto3.client("s3", region_name=config.AWS_REGION)


def object_exists(key: str) -> bool:
    """
    Return True if an object exists at `s3://<bucket>/<key>`.

    Used by the worker's idempotency guard: before reprocessing a job we check
    if `results/<job-id>/result.dcm` is already there — if yes, the job was
    already completed on a previous delivery and we skip the expensive path.
    """
    s3 = _client()
    try:
        s3.head_object(Bucket=config.S3_BUCKET, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        # Any other error (AccessDenied, network, …) should not be silently
        # swallowed — propagate so the worker treats it as infra-level and
        # leaves the SQS message for retry.
        raise



def download_prefix(s3_prefix: str, local_dir: Path) -> None:
    """Download all objects under *s3_prefix* into *local_dir*."""
    s3 = _client()
    local_dir.mkdir(parents=True, exist_ok=True)

    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=config.S3_BUCKET, Prefix=s3_prefix)

    downloaded = 0
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Preserve relative path inside the prefix
            relative = key[len(s3_prefix):]
            if not relative:
                continue  # skip the "directory" placeholder object
            dest = local_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            logger.debug("S3 download: s3://%s/%s → %s", config.S3_BUCKET, key, dest)
            s3.download_file(config.S3_BUCKET, key, str(dest))
            downloaded += 1

    logger.info("Downloaded %d file(s) from s3://%s/%s", downloaded, config.S3_BUCKET, s3_prefix)


def upload_directory(local_dir: Path, s3_prefix: str) -> None:
    """Upload all files in *local_dir* (recursively) to *s3_prefix*."""
    s3 = _client()

    uploaded = 0
    for local_file in sorted(local_dir.rglob("*")):
        if not local_file.is_file():
            continue
        relative = local_file.relative_to(local_dir)
        key = f"{s3_prefix}{relative.as_posix()}"
        logger.debug("S3 upload: %s → s3://%s/%s", local_file, config.S3_BUCKET, key)
        s3.upload_file(str(local_file), config.S3_BUCKET, key)
        uploaded += 1

    logger.info("Uploaded %d file(s) to s3://%s/%s", uploaded, config.S3_BUCKET, s3_prefix)


def upload_failure_artifact(job_id: str, error_text: str) -> None:
    """
    Upload a plain-text error description to s3://<bucket>/failed/<job-id>/error.txt
    for operations / debugging visibility. Never raises — a failed upload must
    not block the rest of the error-handling flow.
    """
    key = f"failed/{job_id}/error.txt"
    try:
        s3 = _client()
        s3.put_object(
            Bucket=config.S3_BUCKET,
            Key=key,
            Body=error_text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        logger.info("Uploaded failure artifact → s3://%s/%s", config.S3_BUCKET, key)
    except Exception:
        logger.exception("Could not upload failure artifact to s3://%s/%s", config.S3_BUCKET, key)
