"""
Central configuration — reads from environment variables (or a .env file).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root if present
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Required environment variable '{key}' is not set.")
    return value


AWS_REGION: str = os.getenv("AWS_REGION", "eu-west-1")
S3_BUCKET: str = os.getenv("S3_BUCKET", "appway-bridge-prod")
JOBS_QUEUE_URL: str = _require("JOBS_QUEUE_URL")
RESULTS_QUEUE_URL: str = _require("RESULTS_QUEUE_URL")
WORK_DIR: Path = Path(os.getenv("WORK_DIR", "./workdir"))

# SNS topic ARN used for operator error notifications.
# Optional — if unset, the worker simply skips the SNS publish (the error ePDF
# is still sent back to the clinician via the normal AppWay path).
ERROR_TOPIC_ARN: str = os.getenv("ERROR_TOPIC_ARN", "")

# --- B8: SQS visibility-timeout heartbeat ---------------------------------
# While a job is being processed we periodically call ChangeMessageVisibility
# on its SQS message so it does NOT become visible again to another worker
# before we finish. Safe defaults:
#   INTERVAL   = 25 s  (how often we send the heartbeat)
#   EXTENSION  = 60 s  (new visibility window each heartbeat sets)
# Each heartbeat resets the window to EXTENSION seconds from "now", so as long
# as INTERVAL < EXTENSION the message will never re-appear mid-processing.
SQS_HEARTBEAT_INTERVAL: int = int(os.getenv("SQS_HEARTBEAT_INTERVAL", "25"))
SQS_HEARTBEAT_EXTENSION: int = int(os.getenv("SQS_HEARTBEAT_EXTENSION", "60"))

# --- YOLO AI model (MyopicCNV+) -------------------------------------------
# The fine-tuned Ultralytics YOLO .pt weight file lives in a private S3 bucket.
# On first use the worker downloads it once to MODEL_LOCAL_PATH and caches it
# in-memory for subsequent jobs (singleton). Set these env vars to override
# for dev / staging / alt models.
MODEL_S3_BUCKET: str = os.getenv("MODEL_S3_BUCKET", "ray-bucket-ai-models")
MODEL_S3_KEY: str = os.getenv("MODEL_S3_KEY", "yolo-april2024/fine_tuned_Mar24.pt")
MODEL_LOCAL_PATH: Path = Path(
    os.getenv(
        "MODEL_LOCAL_PATH",
        "/home/ubuntu/appway-backend/models/fine_tuned_Mar24.pt",
    )
)

# Inference hyper-parameters (matched to the Streamlit webapp for parity)
YOLO_CONF_THRESHOLD: float = float(os.getenv("YOLO_CONF_THRESHOLD", "0.535"))
YOLO_IOU_THRESHOLD: float = float(os.getenv("YOLO_IOU_THRESHOLD", "0.7"))

# Input image size fed to the YOLO network. Heidelberg Spectralis volumes
# arrive as 496 × 512 B-scans; YOLO letterbox-resizes each frame to
# imgsz × imgsz internally. 640 matches the Ultralytics default used when
# the .pt weight was fine-tuned (/home/ubuntu/mcnv/src/web_pages/
# inference_yolo.py also leaves imgsz unset → defaults to 640), so we
# explicitly pin the same value here for forward-compatibility. Override
# only if you fine-tune a new model at a different resolution.
YOLO_IMGSZ: int = int(os.getenv("YOLO_IMGSZ", "640"))

# Target size for PNG frames fed to YOLO. The model was trained on HEYEX
# TIFF exports at 1008 × 596 (see /home/ubuntu/mcnv/src/web_pages/
# helpers.py → list_of_images(): the only preprocessing is PIL
# .convert("RGB") + save as JPEG — no resize). To avoid a
# training/inference domain shift we resize every DICOM-extracted B-scan
# (natively 496 × 512) to this same (width, height) and save as RGB PNG
# before inference. Override if a future model is re-trained on a
# different source resolution.
TRAIN_IMAGE_WIDTH: int = int(os.getenv("TRAIN_IMAGE_WIDTH", "1008"))
TRAIN_IMAGE_HEIGHT: int = int(os.getenv("TRAIN_IMAGE_HEIGHT", "596"))

# --- Clinical Trial Module (B6) -------------------------------------------
# Heidelberg (MedicalCommunications, 2026-04-29) confirmed our non-clinical
# AI solution must populate the DICOM Clinical Trial Subject Module
# (Table C.7-2b) and Clinical Trial Series Module (Table C.7-5b) exactly as
# the DICOM standard requires. The *version suffix* of
# ClinicalTrialProtocolID (0012,0020) — rendered as
#   f"MYOPICCNV-APPWAY-{CLINICAL_TRIAL_PROTOCOL_VERSION}"
# — is environment-driven so ops can bump it (e.g. "1.0.0" → "1.1.0") whenever a
# new model/protocol revision is registered with AppWay, without a code
# change. Use semver notation (e.g. 1.0.0, 1.1.0, 2.0.0); the value must
# be a valid DICOM LO string (≤ 64 chars, no backslashes or control chars).
CLINICAL_TRIAL_PROTOCOL_VERSION: str = os.getenv("CLINICAL_TRIAL_PROTOCOL_VERSION", "1.0.0")

# ─────────────────────────────────────────────────────────────────────────────
# Weekly reporting digest
# ─────────────────────────────────────────────────────────────────────────────

# Comma-separated list of recipient email addresses for the weekly digest.
# Must be verified in SES (or SES must be out of sandbox for arbitrary addresses).
REPORT_RECIPIENTS: list[str] = [
    a.strip()
    for a in os.getenv("REPORT_RECIPIENTS", "").split(",")
    if a.strip()
]

# Verified SES sender identity (From: address).
REPORT_FROM: str = os.getenv("REPORT_FROM", "darkfenner69@gmail.com")

# How long the per-period images.zip presigned URL is valid for.
REPORT_PRESIGNED_TTL_DAYS: int = int(os.getenv("REPORT_PRESIGNED_TTL_DAYS", "7"))

# S3 prefix where per-job audit JSONs are stored.
# Full path: s3://<S3_BUCKET>/<AUDIT_PREFIX><YYYY>/<MM>/<DD>/<job-id>.json
AUDIT_PREFIX: str = os.getenv("AUDIT_PREFIX", "audit/")

# S3 prefix where generated report PDFs and image zips are stored.
# Full path: s3://<S3_BUCKET>/<REPORT_PREFIX><YYYY-MM-DD>/report.pdf
#                                                          <YYYY-MM-DD>/images.zip
REPORT_PREFIX: str = os.getenv("REPORT_PREFIX", "reports/")

# S3 key for the persistent reporting state (last period end timestamp + history).
REPORT_STATE_KEY: str = os.getenv("REPORT_STATE_KEY", "reports/state.json")

# Subject prefix for outgoing report emails.
REPORT_SUBJECT_PREFIX: str = os.getenv("REPORT_SUBJECT_PREFIX", "mCNV+ reporting at")

# job_id prefix convention for test jobs (set by inject_job.sh --job-id test-...).
# Any audit JSON whose job_id starts with this value is treated as a test and
# excluded from the official weekly report (but shown in manual_report by default).
REPORT_TEST_JOB_PREFIX: str = os.getenv("REPORT_TEST_JOB_PREFIX", "test-")
