"""
AppWay backend worker — infinite SQS poll loop.

Flow (matches sequence diagram steps 10–16):
  10. Poll appway-jobs (long-poll)
  11. Receive job message
  12. Download S3 incoming/<job-id>/
  13. Process payload
  14. Upload output to S3 results/<job-id>/
  15. Send result message to appway-results
  16. Delete job message from appway-jobs
  → back to step 10

Error handling (spec §9.2 — error forwarding):
  If processing fails at the application level, the worker MUST still
  forward an error result ePDF back to the customer, write a failure
  artifact to S3 for ops visibility, notify operators via SNS, and delete
  the SQS message (so it does not loop).
  Only true infrastructure failures (unable to even send the error result)
  are left for SQS → DLQ.
"""
import logging
import shutil
import sys
import threading
import traceback
from pathlib import Path

from . import config
from . import s3_utils, sqs_utils, sns_utils
from .processor import process
from .epdf_generator import generate_error_epdf_dcm


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# B8 — SQS visibility-timeout heartbeat
# ─────────────────────────────────────────────────────────────────────────

class VisibilityHeartbeat:
    """
    Context manager that periodically extends the SQS visibility timeout of
    an in-flight job message so a long-running job does NOT become visible
    to another worker before we finish.

    Usage:
        with VisibilityHeartbeat(receipt_handle, job_id):
            do_long_work()  # download + process + upload

    Design notes:
      - Uses a background thread + `threading.Event` so `stop()` wakes the
        thread immediately rather than sleeping for the full interval.
      - Heartbeat failures are logged but NEVER re-raised: the worst case
        is the message becomes re-delivered, and the B7 idempotency guard
        already protects against duplicate processing.
      - After three consecutive heartbeat failures the thread exits silently
        (assumes the message is gone or the connection is broken — let SQS
        do its thing).
      - No-op if `receipt_handle` is falsy.
    """

    def __init__(
        self,
        receipt_handle: str,
        job_id: str,
        interval: int | None = None,
        extension: int | None = None,
    ) -> None:
        self._receipt_handle = receipt_handle
        self._job_id = job_id
        self._interval = interval if interval is not None else config.SQS_HEARTBEAT_INTERVAL
        self._extension = extension if extension is not None else config.SQS_HEARTBEAT_EXTENSION
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop.wait(self._interval):
            try:
                sqs_utils.extend_visibility(self._receipt_handle, self._extension)
                consecutive_failures = 0
                logger.debug(
                    "[%s] Heartbeat: visibility extended to %ds",
                    self._job_id, self._extension,
                )
            except Exception:
                consecutive_failures += 1
                logger.warning(
                    "[%s] Heartbeat failed (%d consecutive)",
                    self._job_id, consecutive_failures,
                    exc_info=True,
                )
                if consecutive_failures >= 3:
                    logger.warning(
                        "[%s] Heartbeat giving up after 3 consecutive failures — "
                        "B7 idempotency guard will catch any re-delivery.",
                        self._job_id,
                    )
                    return

    def __enter__(self) -> "VisibilityHeartbeat":
        if not self._receipt_handle:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self._job_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[%s] Heartbeat started (interval=%ds, extension=%ds)",
            self._job_id, self._interval, self._extension,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        logger.debug("[%s] Heartbeat stopped", self._job_id)


def _forward_error_result(
    job_id: str,
    input_dir: Path,
    output_dir: Path,
    result_prefix: str,
    receipt_handle: str,
    exc: BaseException,
) -> None:
    """
    Spec §9.2 error-forwarding path:
      1. Build an error ePDF (result.dcm with an error PDF inside)
      2. Upload it to s3://.../results/<job-id>/
      3. Upload a plain-text failure artifact to s3://.../failed/<job-id>/error.txt
      4. Send the result message on appway-results
      5. Publish an SNS notification to the operator topic
      6. Delete the SQS job message so it does NOT loop

    This function is wrapped in its own try/except by the caller: if any of
    these AWS calls fail, the outer code path leaves the SQS message for
    retry (→ DLQ) and re-raises.
    """
    error_message = f"{type(exc).__name__}: {exc}"
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    # 1. Build error ePDF
    output_dir.mkdir(parents=True, exist_ok=True)
    error_dcm = output_dir / "result.dcm"
    generate_error_epdf_dcm(job_id, input_dir, error_dcm, error_message)

    # 2. Upload error result to S3 (so AppWay result consumer can pick it up)
    logger.info("[%s] Uploading ERROR result to s3://%s/%s", job_id, config.S3_BUCKET, result_prefix)
    s3_utils.upload_directory(output_dir, result_prefix)

    # 3. Upload human-readable failure artifact (never raises)
    artifact = (
        f"Job ID: {job_id}\n"
        f"Error type: {type(exc).__name__}\n"
        f"Error message: {exc}\n\n"
        f"Traceback:\n{tb_text}"
    )
    s3_utils.upload_failure_artifact(job_id, artifact)

    # 4. Send result notification so the AppWay result consumer picks it up
    sqs_utils.send_result_message(job_id, result_prefix)

    # 5. Notify operator (never raises)
    sns_utils.publish_error_notification(
        job_id=job_id,
        error_message=str(exc),
        error_type=type(exc).__name__,
        traceback_text=tb_text,
    )

    # 6. Delete job message — application-level error has been fully handled
    sqs_utils.delete_message(receipt_handle)


def _handle_job(msg: dict) -> None:
    body = msg["ParsedBody"]
    receipt_handle = msg["ReceiptHandle"]

    # --- Extract job identity ---
    job_id: str = body.get("job_id") or body.get("folder_name", "")
    if not job_id:
        logger.error("Message has no 'job_id' or 'folder_name' field — skipping. Body: %s", body)
        return

    # The publisher sends the S3 prefix; fall back to the conventional path.
    input_prefix: str = body.get("input_prefix") or f"incoming/{job_id}/"
    logger.info("=== New job: %s (input prefix: %s) ===", job_id, input_prefix)

    # --- Local work directories ---
    job_dir = config.WORK_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    result_prefix = f"results/{job_id}/"
    result_key = f"{result_prefix}result.dcm"

    # --- Idempotency guard (B7) ---
    # SQS is at-least-once. If `result.dcm` already exists at the expected
    # key then this job was fully processed on a previous delivery but the
    # DeleteMessage call did not land. Just re-notify the result consumer
    # and delete the job message — never reprocess.
    try:
        already_done = s3_utils.object_exists(result_key)
    except Exception:
        logger.exception(
            "[%s] Could not check idempotency key s3://%s/%s — treating as infra failure",
            job_id,
            config.S3_BUCKET,
            result_key,
        )
        # Leave SQS message for retry → DLQ.
        return
    if already_done:
        logger.info(
            "[%s] Result already present at s3://%s/%s — re-notifying and skipping reprocess.",
            job_id,
            config.S3_BUCKET,
            result_key,
        )
        try:
            sqs_utils.send_result_message(job_id, result_prefix)
            sqs_utils.delete_message(receipt_handle)
        except Exception:
            logger.exception(
                "[%s] Failed to re-notify / delete on idempotent path — leaving for retry.",
                job_id,
            )
        return

    try:
        # B8 — Keep the SQS message in-flight while we work.
        # Covers download → process → upload → result-send. As soon as the
        # `with` block exits we can safely call delete_message because SQS
        # still considers the message "in-flight" until we either delete it
        # or the most recent extension window expires.
        with VisibilityHeartbeat(receipt_handle, job_id):
            # Step 12 — download input from S3
            logger.info("[%s] Downloading input from s3://%s/%s", job_id, config.S3_BUCKET, input_prefix)
            s3_utils.download_prefix(input_prefix, input_dir)

            # Step 13 — process
            logger.info("[%s] Processing…", job_id)
            process(job_id, input_dir, output_dir)

            # Step 14 — upload output to S3
            logger.info("[%s] Uploading output to s3://%s/%s", job_id, config.S3_BUCKET, result_prefix)
            s3_utils.upload_directory(output_dir, result_prefix)

            # Step 15 — send result message to appway-results
            sqs_utils.send_result_message(job_id, result_prefix)

        # Step 16 — delete job message from appway-jobs (heartbeat stopped)
        sqs_utils.delete_message(receipt_handle)

        logger.info("[%s] Job complete ✓", job_id)

        # B4 — workdir cleanup on success
        _cleanup_workdir(job_dir, job_id)

    except Exception as exc:
        # Application-level failure — forward an error result to the customer
        # per spec §9.2, alert operators via SNS, and delete the SQS message.
        logger.exception(
            "[%s] Job failed at application level — forwarding error result to AppWay",
            job_id,
        )
        try:
            _forward_error_result(
                job_id=job_id,
                input_dir=input_dir,
                output_dir=output_dir,
                result_prefix=result_prefix,
                receipt_handle=receipt_handle,
                exc=exc,
            )
            logger.info("[%s] Error result forwarded; SQS message deleted.", job_id)
            # B4 — workdir cleanup on handled application failure
            _cleanup_workdir(job_dir, job_id)
        except Exception:
            # Even error-forwarding failed → this is infrastructure-level.
            # Leave the message for SQS retry → eventually DLQ → CloudWatch
            # alarm → SNS email. Do NOT clean up the workdir here — we may
            # still want to inspect its contents while the job is retrying.
            logger.exception(
                "[%s] Could not forward error result — leaving SQS message for retry / DLQ",
                job_id,
            )


def _cleanup_workdir(job_dir: Path, job_id: str) -> None:
    """Delete the per-job working directory. Never raises."""
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.info("[%s] Removed workdir %s", job_id, job_dir)
    except Exception:
        # ignore_errors=True already swallows most issues; this catch is defence in depth.
        logger.exception("[%s] Workdir cleanup failed for %s", job_id, job_dir)


def main() -> None:
    """Entry point — runs forever."""
    logger.info("AppWay backend worker starting.")
    logger.info(
        "Config: region=%s  bucket=%s  jobs=%s  results=%s  workdir=%s  error_topic=%s",
        config.AWS_REGION,
        config.S3_BUCKET,
        config.JOBS_QUEUE_URL,
        config.RESULTS_QUEUE_URL,
        config.WORK_DIR,
        config.ERROR_TOPIC_ARN or "(not configured)",
    )
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Polling %s (long-poll %ds)…", config.JOBS_QUEUE_URL, sqs_utils.LONG_POLL_WAIT)

    while True:
        try:
            msg = sqs_utils.receive_job_message()
            if msg is None:
                # No message in this window — poll again immediately
                continue
            _handle_job(msg)
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down.")
            break
        except Exception:
            # Unexpected error in the poll loop itself — log and keep going
            logger.exception("Unexpected error in poll loop — continuing.")
