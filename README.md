# appway-backend

> **MyopicCNV+** — AI-powered Myopic Choroidal Neovascularisation (mCNV) detection backend  
> Running on AWS (EC2 + SQS + S3 + SES) · Python 3.13 · YOLO v8 · ReportLab

---

## What it does

Heidelberg Engineering's **AppWay bridge** drops OCT DICOM files into an S3 bucket and enqueues a job message. This backend:

1. **Polls** an SQS queue for jobs (long-poll, infinite loop)
2. **Downloads** the DICOM files from S3
3. **Runs YOLO v8 inference** to detect mCNV lesions
4. **Generates a branded 3-page PDF report** (verdict, image gallery, per-image results table)
5. **Packages** the PDF into an AppWay-compliant ePDF DICOM (`result.dcm`)
6. **Uploads** the result back to S3 and notifies the AppWay result queue
7. **Uploads report assets** (PDF + PNG images) to S3 for the weekly digest
8. **Emails a weekly digest** every Sunday 06:00 UTC with statistics + presigned download link

---

## Architecture overview

```
Heidelberg HEYEX
     │  (AppWay bridge)
     ▼
S3: incoming/<job-id>/*.dcm
     │
     ▼
SQS: appway-jobs
     │
     ▼
┌─────────────────────────────────────────────┐
│  appway-worker.service  (this repo)         │
│                                             │
│  1. download DICOMs from S3                 │
│  2. extract PNGs → run YOLO inference       │
│  3. generate branded PDF → wrap as ePDF DCM │
│  4. upload result.dcm → S3 results/<job>/   │
│  5. upload assets (PDF+PNGs) → S3 assets/   │
│  6. notify appway-results SQS queue         │
│  7. write audit record to JSONL             │
└─────────────────────────────────────────────┘
     │
     ▼
SQS: appway-results  →  Heidelberg AppWay picks up result.dcm
S3:  results/<job>/result.dcm

Weekly (Sunday 06:00 UTC):
┌─────────────────────────────────────────────┐
│  appway-weekly-report.service               │
│                                             │
│  1. read audit log for past 7 days          │
│  2. stream assets from S3 → build zip       │
│  3. generate digest PDF (stats + tables)    │
│  4. email via SES: PDF attached,            │
│     images.zip linked (presigned URL)       │
└─────────────────────────────────────────────┘
```

---

## Repository layout

```
appway_backend/
  config.py            — env-var config (loaded from .env)
  worker.py            — main SQS poll loop + job orchestration
  processor.py         — DICOM → PNG extraction + inference + audit
  inference.py         — YOLO v8 inference wrapper
  epdf_generator.py    — ePDF DICOM builder (result + error paths)
  pdf_report.py        — PDF content renderer
  s3_utils.py          — S3 download / upload helpers
  sqs_utils.py         — SQS receive / delete / heartbeat helpers
  sns_utils.py         — SNS operator-alert publisher
  report/              — PDF template + generator (ReportLab + PyMuPDF)
    generator.py       — build_pdf(job, out_path)
    templates.py       — render designer .ai to PNG chrome (run once)
    assets/            — Montserrat fonts, page-template PNGs, icons
  reporting/           — Weekly digest subsystem
    weekly_report.py   — entry point (run by systemd timer)
    core.py            — orchestrate: audit → pdf → bundle → email
    audit.py           — read / write JSONL audit log
    bundle.py          — build images.zip from S3 assets
    pdf.py             — generate digest PDF report
    email.py           — send via SES (raw MIME)
    state.py           — last-run state file (idempotency)
    manual_report.py   — CLI for ad-hoc reports + dry-run previews

docs/                  — Operator + developer runbooks
scripts/               — Helper scripts (inject test jobs, cleanup, RDP, etc.)
systemd/               — Unit files (worker + weekly report + prune timer)
logs/                  — Log directory (worker.log, workflow.logs)
main.py                — Entry point (calls worker.main())
pyproject.toml         — Dependencies (uv)
```

---

## S3 bucket layout

```
appway-bridge-prod/
  incoming/<job-id>/          ← DICOM input from Heidelberg AppWay
  results/<job-id>/
    result.dcm                ← ePDF DICOM (AppWay contract artefact)
    assets/
      result.pdf              ← Human-readable PDF report
      <stem>/<frame>.png      ← Extracted B-scan images
  failed/<job-id>/error.txt   ← Failure artefact (ops visibility)
  processed/<job-id>/         ← Moved by AppWay bridge after pickup
  reports/<YYYY-MM-DD>/
    images.zip                ← Weekly bundle (presigned URL in digest email)
```

---

## Managed systemd services

| Unit file | What | Schedule |
|---|---|---|
| `appway-worker.service` | Main worker — SQS poll loop | Always running (`Restart=on-failure`) |
| `appway-weekly-report.service` | Weekly digest (oneshot, called by timer) | — |
| `appway-weekly-report.timer` | Fires the weekly digest | Sunday 06:00 UTC (`Persistent=true`) |
| `appway-prune-outputs.service` | Local outputs prune (oneshot, called by timer) | — |
| `appway-prune-outputs.timer` | Fires the nightly prune | Nightly 03:00 UTC (`Persistent=true`) |

Quick commands:

```bash
# Worker
sudo systemctl status appway-worker.service
sudo journalctl -u appway-worker.service -f

# Weekly report (run manually)
sudo systemctl start appway-weekly-report.service
tail -f /var/log/appway-weekly-report.log

# Prune (run manually)
sudo systemctl start appway-prune-outputs.service
cat /var/log/appway-prune.log

# All timers
systemctl list-timers --no-pager | grep appway
```

---

## Environment variables (`.env`)

| Variable | Description |
|---|---|
| `AWS_REGION` | AWS region (e.g. `eu-west-1`) |
| `S3_BUCKET` | S3 bucket name |
| `JOBS_QUEUE_URL` | SQS URL for incoming jobs (`appway-jobs`) |
| `RESULTS_QUEUE_URL` | SQS URL for results (`appway-results`) |
| `ERROR_TOPIC_ARN` | SNS topic ARN for operator alerts |
| `WORK_DIR` | Local scratch dir for job processing |
| `CLINICAL_TRIAL_PROTOCOL_VERSION` | Version string stamped on reports |
| `REPORT_RECIPIENTS` | Comma-separated email list for weekly digest |

---

## Logs

| File | Content |
|---|---|
| `/var/log/appway-worker.log` | Worker stdout/stderr (all job activity) |
| `/var/log/appway-weekly-report.log` | Weekly digest runs |
| `/var/log/appway-prune.log` | Nightly local-output prune |

All three are rotated weekly, 8 rotations kept, compressed (`/etc/logrotate.d/appway`).

---

## PDF report

The 3-page branded report uses a **template-overlay** strategy:

1. The designer's `.ai` source is stripped of all text (PyMuPDF redaction) to produce page-chrome PNGs (`assets/page_template_*.png`)
2. `report/generator.py` stamps the chrome as background (ReportLab) then redraws all dynamic content at the exact designer coordinates
3. The verdict card gradient is rasterised in PIL at 4× oversample then stamped as a transparent PNG

See [`docs/pdf-layout.md`](docs/pdf-layout.md) for the full coordinate reference.

---

## Weekly reporting digest

Every Sunday at 06:00 UTC the digest:

- Reads the past 7 days from the JSONL audit log
- Builds a summary PDF (Table A: per-job, Table B: cumulative, Table C: test jobs)
- Streams per-job assets (PDF + PNGs) directly from S3 into a zip
- Uploads the zip to `s3://<bucket>/reports/<date>/images.zip`
- Emails via SES: digest PDF attached, presigned download link in HTML body

See [`docs/reporting.md`](docs/reporting.md) for the operator runbook.

---

## Running a manual report

```bash
cd /home/ubuntu/appway-backend

# Dry-run (no email — saves PDF + zip to outputs/_report_preview/)
uv run python -m appway_backend.reporting.manual_report

# Real send (uses REPORT_RECIPIENTS from .env)
uv run python -m appway_backend.reporting.manual_report --send

# Custom date range
uv run python -m appway_backend.reporting.manual_report \
    --from 2026-06-01 --to 2026-06-07 --send
```

---

## Injecting a test job

```bash
# Inject a synthetic test DICOM job into the SQS queue
bash scripts/inject_job.sh

# Clean up test job outputs from S3 + local outputs/
bash scripts/cleanup_test_jobs.sh
```

---

## IAM

The EC2 instance runs as `EC2AppWayBackendRole` (no embedded keys). Required permissions are documented in:

- [`docs/iam-ec2apwaybackendrole-policy.json`](docs/iam-ec2apwaybackendrole-policy.json) — worker + S3 + SQS + SNS
- [`docs/iam-weekly-report-policy.json`](docs/iam-weekly-report-policy.json) — SES send permissions

---

## Development

```bash
# Install dependencies
uv sync

# Run worker locally (needs .env with valid AWS creds)
uv run python main.py

# Generate PDF preview (no AWS needed)
uv run python -m appway_backend.report.preview
```

---

## Docs index

| Document | Audience |
|---|---|
| [`docs/workflow.md`](docs/workflow.md) | End-to-end system workflow |
| [`docs/backend.md`](docs/backend.md) | Backend developer reference |
| [`docs/reporting.md`](docs/reporting.md) | Weekly digest operator runbook |
| [`docs/appway.md`](docs/appway.md) | AppWay integration notes |
| [`docs/appway-windows-ec2.md`](docs/appway-windows-ec2.md) | Windows EC2 setup |
| [`docs/heyex-daily.md`](docs/heyex-daily.md) | Daily HEYEX operator procedure |
| [`docs/heidelberg-remote-session.md`](docs/heidelberg-remote-session.md) | Remote session setup |
| [`docs/klaus-procmon-runbook.md`](docs/klaus-procmon-runbook.md) | Klaus Process Monitor runbook |
| [`docs/pdf-layout.md`](docs/pdf-layout.md) | PDF coordinate reference |
| [`docs/next-steps.md`](docs/next-steps.md) | Planned improvements |
| [`scripts/README.md`](scripts/README.md) | Scripts reference |
