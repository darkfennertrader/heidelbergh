"""
SQS helpers — receive, send, and delete messages.
"""
import json
import logging
from typing import Optional

import boto3

from . import config

logger = logging.getLogger(__name__)

LONG_POLL_WAIT = 20  # seconds — SQS maximum


def _client():
    return boto3.client("sqs", region_name=config.AWS_REGION)


def receive_job_message() -> Optional[dict]:
    """
    Long-poll appway-jobs for a single message.
    Returns the raw SQS message dict (with Body parsed as JSON), or None if no
    message arrived within the wait window.
    """
    sqs = _client()
    response = sqs.receive_message(
        QueueUrl=config.JOBS_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=LONG_POLL_WAIT,
        AttributeNames=["All"],
        MessageAttributeNames=["All"],
    )
    messages = response.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    try:
        msg["ParsedBody"] = json.loads(msg["Body"])
    except json.JSONDecodeError:
        logger.warning("Could not parse SQS message body as JSON: %s", msg["Body"])
        msg["ParsedBody"] = {}
    return msg


def send_result_message(job_id: str, result_prefix: str) -> None:
    """Send a completion message to appway-results."""
    sqs = _client()
    body = json.dumps({"job_id": job_id, "result_prefix": result_prefix})
    sqs.send_message(QueueUrl=config.RESULTS_QUEUE_URL, MessageBody=body)
    logger.info("Sent result message for job %s → %s", job_id, result_prefix)


def delete_message(receipt_handle: str) -> None:
    """Delete a message from appway-jobs by its receipt handle."""
    sqs = _client()
    sqs.delete_message(QueueUrl=config.JOBS_QUEUE_URL, ReceiptHandle=receipt_handle)
    logger.debug("Deleted SQS message (receipt: %s…)", receipt_handle[:20])


def extend_visibility(receipt_handle: str, seconds: int) -> None:
    """
    Extend the visibility timeout of an in-flight job message on appway-jobs.

    Called by the B8 heartbeat so long-running jobs do not become visible
    again to a second worker before we finish. `seconds` is the NEW window
    measured from now (it is NOT added on top of the remaining time).
    """
    sqs = _client()
    sqs.change_message_visibility(
        QueueUrl=config.JOBS_QUEUE_URL,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=seconds,
    )
    logger.debug(
        "Extended SQS visibility to %ds (receipt: %s…)",
        seconds, receipt_handle[:20],
    )
