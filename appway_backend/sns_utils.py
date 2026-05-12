"""
SNS helpers — publish operator error notifications.

The worker uses this to immediately alert operators (via email subscription
to the SNS topic) when a job fails at the application level. This is
complementary to the CloudWatch DLQ alarm, which fires only when messages
reach the dead-letter queue after repeated infrastructure failures.
"""
import logging
import socket
from datetime import datetime, timezone
from typing import Optional

import boto3

from . import config

logger = logging.getLogger(__name__)


def _client():
    return boto3.client("sns", region_name=config.AWS_REGION)


def publish_error_notification(
    job_id: str,
    error_message: str,
    error_type: Optional[str] = None,
    traceback_text: Optional[str] = None,
) -> None:
    """
    Publish an error notification to the configured SNS topic.

    No-op (with a warning log) when ERROR_TOPIC_ARN is not configured — this
    keeps the worker runnable in local / dev environments without SNS.

    Never raises — failing to notify must not prevent the rest of the error
    handling flow (error ePDF upload, SQS message deletion, etc.).
    """
    if not config.ERROR_TOPIC_ARN:
        logger.warning(
            "[%s] ERROR_TOPIC_ARN not configured — skipping SNS notification.",
            job_id,
        )
        return

    subject = f"[AppWay] Job {job_id} failed"
    # SNS email subject is limited to 100 ASCII characters.
    if len(subject) > 99:
        subject = subject[:99]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    host = socket.gethostname()

    lines = [
        "AppWay backend worker — application-level job failure",
        "",
        f"Job ID      : {job_id}",
        f"Timestamp   : {now}",
        f"Worker host : {host}",
        f"Region      : {config.AWS_REGION}",
        f"S3 bucket   : {config.S3_BUCKET}",
        "",
        f"Error type  : {error_type or 'Exception'}",
        "Error       :",
        f"  {error_message}",
    ]
    if traceback_text:
        lines += ["", "Traceback:", traceback_text]
    lines += [
        "",
        "An error result ePDF has been generated and forwarded to the",
        "customer via the normal AppWay result path. The SQS job message",
        "has been deleted to prevent redelivery.",
        "",
        "Check S3 for the failure artifact:",
        f"  s3://{config.S3_BUCKET}/failed/{job_id}/",
    ]
    message = "\n".join(lines)

    try:
        sns = _client()
        resp = sns.publish(
            TopicArn=config.ERROR_TOPIC_ARN,
            Subject=subject,
            Message=message,
        )
        logger.info(
            "[%s] SNS error notification sent (MessageId=%s)",
            job_id,
            resp.get("MessageId", "?"),
        )
    except Exception:
        logger.exception("[%s] Failed to publish SNS error notification", job_id)
