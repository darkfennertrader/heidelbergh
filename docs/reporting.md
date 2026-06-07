# mCNV+ Reporting Digest — Operator Runbook

---

## How it works in practice — chronological walkthrough

> This section answers *"what actually happens, in what order, when?"*
> The reference details (flags, config keys, S3 layout, troubleshooting) are in the sections below.

### TL;DR — your day-to-day commands

| I want to… | Run this |
|---|---|
| Preview the next report without sending anything | `uv run python -m appway_backend.reporting.manual_report --dry-run` |
| Send a report to everyone in `REPORT_RECIPIENTS` (.env) | `uv run python -m appway_backend.reporting.manual_report` |
| Send a report **only to myself** (override .env) | `uv run python -m appway_backend.reporting.manual_report --recipients me@example.com` |
| Send to myself, clinical only (no test Table C) | `uv run python -m appway_backend.reporting.manual_report --recipients me@example.com --no-tests` |
| Re-send a specific past period to myself | `uv run python -m appway_backend.reporting.manual_report --recipients me@example.com --from 2026-05-01 --to 2026-05-31` |
| Trigger the official weekly report immediately (advances state) | `sudo systemctl start appway-weekly-report.service` |
| Check when the next Sunday run is scheduled | `systemctl list-timers appway-weekly-report.timer` |
| Tail the weekly-report log | `tail -f /var/log/appway-weekly-report.log` |

---

### 1 · Every time a job completes (automatic — no action needed)

When the worker finishes processing a DICOM study it automatically:

1. Writes an audit record to S3:
   ```
   s3://appway-bridge-prod/audit/YYYY/MM/DD/<job-id>.json
   ```
   Each record contains: `job_id`, `completed_at`, `accession_number`,
   `study_instance_uid`, `n_images`, `n_positive`, `n_negative`, `verdict`,
   `processing_time_s`, `is_test`.

2. Uploads report assets to S3 alongside the clinical `result.dcm`:
   ```
   s3://appway-bridge-prod/results/<job-id>/assets/result.pdf
   s3://appway-bridge-prod/results/<job-id>/assets/<stem>/<frame>.png
   ```
   These are streamed directly into the weekly `images.zip` — the local
   `outputs/<job-id>/` directory is **not** required by the digest.

**`is_test`** is `true` when `job_id` starts with `test-` (jobs injected via
`scripts/inject_job.sh`).  Clinical jobs (`final-…`) always have `is_test=false`.

You never have to do anything here — it's fully automatic.

---

### 2 · Sunday 06:00 UTC — the automatic weekly report

The systemd timer fires `weekly_report.py`, which runs this sequence:

1. **Read `reports/state.json`** from S3 to find `last_period_end`
   (defaults to `2026-01-01 00:00 UTC` on the very first run).
2. **Determine the window:** `last_period_end → now`.
3. **Scan all audit JSONs** in that window — clinical jobs only (`is_test=false`).
4. **Build `images.zip` in memory:**
   - For each clinical job: a subfolder `clinical/<Accession # · short-job-id>/`
     containing `result.pdf` + all OCT PNGs.
   - `manifest.csv` + `README.txt` at the root.
   - Upload to `s3://appway-bridge-prod/reports/YYYY-MM-DD/images.zip`.
5. **Generate a 7-day presigned URL** for the zip.
6. **Render digest PDF** (`reports/YYYY-MM-DD/report.pdf`):
   - **Table A** — one row per clinical analysis in this period.
   - **Table B** — cumulative one-row-per-past-period summary (avg proc time).
   - *(Table C is omitted — weekly report never shows test data.)*
7. **Send email via SES:**
   - Subject: `mCNV+ reporting at YYYY-MM-DD`
   - HTML body: headline numbers + big "Download images.zip" button.
   - Attachment: digest PDF (`mcnv-digest-YYYY-MM-DD.pdf`).
8. **Write updated `state.json`** — `last_period_end` is set to *now*, and this
   period's summary is appended to `history[]` (feeds Table B in future runs).

> **This is the only command that advances `state.json`.**
> Manual reports never touch it.

---

### 3 · When you want to look at things manually (no email sent)

```bash
cd /home/ubuntu/appway-backend

# Preview with test data included (default)
uv run python -m appway_backend.reporting.manual_report --dry-run

# Preview clinical analyses only
uv run python -m appway_backend.reporting.manual_report --dry-run --no-tests
```

Nothing is uploaded to S3, nothing is emailed.  Two local files are written:

```
outputs/_report_preview/report.pdf    ← open this to see Tables A, B (and C if tests exist)
outputs/_report_preview/images.zip    ← open this to inspect the image bundle
```

The terminal also prints a plain-text summary (period, # analyses, # pos,
# neg, avg processing time) so you can verify counts at a glance.

---

### 4 · When you want to actually email a report yourself

```bash
# Default — same window as the next Sunday cron, clinical + tests
uv run python -m appway_backend.reporting.manual_report

# Clinical analyses only (omit Table C)
uv run python -m appway_backend.reporting.manual_report --no-tests

# Override recipients (useful for one-off without editing .env)
uv run python -m appway_backend.reporting.manual_report \
    --recipients you@example.com,colleague@example.com

# Re-send a specific historical period (any --from/--to combination)
uv run python -m appway_backend.reporting.manual_report \
    --from 2026-05-01 --to 2026-05-31 \
    --recipients you@example.com
```

> ⚠️ `manual_report` **never** advances `state.json`, regardless of flags.
> The next Sunday cron will still pick up from the same `last_period_end`.

---

### 5 · What the email looks like

| Part | Contents |
|---|---|
| **Subject** | `mCNV+ reporting at 2026-06-08` |
| **HTML body** | Brand banner · headline stats card (analyses / positive / negative / avg proc time) · period line · big blue "Download images.zip" button · footnote if Table C is present |
| **Attachment** | `mcnv-digest-2026-06-08.pdf` |
| **Table A** | Per-analysis rows: Date · ID (`Accession # · short-job-id`) · # Images · # Positive · # Negative · Processing time · Verdict |
| **Table B** | Cumulative rows: Period · # Analyses · # Positive · # Negative · Avg processing time |
| **Table C** | Live test analyses (manual reports only): same columns as Table A, no Verdict column |

The zip layout is:
```
images.zip
├── clinical/
│   ├── ACC001 · a1b2c3d4/
│   │   ├── result.pdf          ← AI digest for this study
│   │   ├── slice_001.png
│   │   └── slice_002.png
│   └── …
├── test/                       ← only present when manual_report includes tests
│   └── …
├── manifest.csv
└── README.txt
```

Image filenames inside each subfolder match the "Per-Image Results" table
inside `result.pdf` for easy cross-referencing.

---

### 6 · Test-data lifecycle

1. `scripts/inject_job.sh` creates jobs with `job_id` starting with `test-`.
2. Audit records for these jobs have `is_test=true`.
3. The **Sunday weekly report** completely ignores them (Tables A + B are always clinical-only).
4. **`manual_report`** includes them by default in **Table C**.
5. Run `scripts/cleanup_test_jobs.sh` to remove test results from S3/local storage.
6. After cleanup, the liveness check (`results/<job-id>/result.dcm` no longer
   exists in S3) automatically drops those rows from Table C on the next manual report.

---

### 7 · One-time setup still required on this machine

The reporting code is complete, but the systemd timer is not yet installed.
Before the first Sunday cron can fire, you need to:

```bash
# 1. Add recipients to .env (required — without this no email is sent)
echo "REPORT_RECIPIENTS=your@email.com" >> /home/ubuntu/appway-backend/.env

# 2. Copy the unit files
sudo cp /home/ubuntu/appway-backend/systemd/appway-weekly-report.service \
        /home/ubuntu/appway-backend/systemd/appway-weekly-report.timer \
        /etc/systemd/system/

# 3. Enable and start the timer
sudo systemctl daemon-reload
sudo systemctl enable --now appway-weekly-report.timer

# 4. Verify
systemctl list-timers appway-weekly-report.timer
```

Once done, the timer is self-maintaining — it will fire every Sunday at 06:00 UTC
and recover from reboots automatically (`Persistent=true`).

---

## Overview

The reporting system produces a weekly email digest of all clinical analyses
processed by the AppWay backend.  It consists of:

| Component | Purpose |
|---|---|
| `appway_backend/reporting/weekly_report.py` | **Official** weekly report — run by systemd timer |
| `appway_backend/reporting/manual_report.py` | **Ad-hoc** report — run by the operator on demand |
| `appway_backend/reporting/core.py` | Shared engine (audit → PDF → zip → email) |
| `systemd/appway-weekly-report.{service,timer}` | Systemd units that fire every Sunday 06:00 UTC |
| `scripts/run-weekly-report.sh` | Shell wrapper called by the service unit |

### What gets sent

Every report email contains:

- **Subject:** `mCNV+ reporting at YYYY-MM-DD`
- **Body (HTML):** headline numbers (analyses / positive / negative / avg proc time),
  period, download button
- **Attachment:** digest PDF with up to three tables:
  - **Table A** — per-analysis rows for the current period (clinical only)
  - **Table B** — cumulative one-row-per-period summary (clinical only)
  - **Table C** — live test analyses currently in S3 *(manual_report only)*
- **Download link:** presigned URL for `images.zip` (7-day TTL)

`images.zip` contains per-analysis subfolders, each with the AI `result.pdf`
+ all OCT PNG images.  Image filenames match the "Per-Image Results" table
inside `result.pdf` for easy cross-referencing.

---

## Configuration

All settings live in `.env` at the project root.  Add these keys:

```bash
# Required for reporting
REPORT_RECIPIENTS=alice@example.com,bob@example.com
REPORT_FROM=darkfenner69@gmail.com          # must be SES-verified

# Optional overrides (defaults shown)
REPORT_PRESIGNED_TTL_DAYS=7
REPORT_SUBJECT_PREFIX=mCNV+ reporting at
AUDIT_PREFIX=audit/
REPORT_PREFIX=reports/
REPORT_STATE_KEY=reports/state.json
REPORT_TEST_JOB_PREFIX=test-
```

---

## Install the systemd timer (one-time setup)

```bash
# 1. Copy the unit files to the systemd directory
sudo cp /home/ubuntu/appway-backend/systemd/appway-weekly-report.service \
        /home/ubuntu/appway-backend/systemd/appway-weekly-report.timer \
        /etc/systemd/system/

# 2. Reload systemd and enable + start the timer
sudo systemctl daemon-reload
sudo systemctl enable --now appway-weekly-report.timer

# 3. Verify the timer is scheduled
systemctl list-timers appway-weekly-report.timer
```

Expected output:
```
NEXT                         LEFT        LAST                         PASSED  UNIT
Sun 2026-06-07 06:00:00 UTC  5 days ago  -                            -       appway-weekly-report.timer
```

---

## Running reports manually

### Dry-run preview (no email, files saved locally)

```bash
cd /home/ubuntu/appway-backend

# Preview with tests (default) — outputs to outputs/_report_preview/
uv run python -m appway_backend.reporting.manual_report --dry-run

# Preview without tests
uv run python -m appway_backend.reporting.manual_report --dry-run --no-tests
```

After running, inspect:
- `outputs/_report_preview/report.pdf` — the digest PDF
- `outputs/_report_preview/images.zip` — the image bundle

### Send an ad-hoc report (uses REPORT_RECIPIENTS from .env)

```bash
# Default window: last_period_end → now (clinical + tests)
uv run python -m appway_backend.reporting.manual_report

# Clinical analyses only (no Table C)
uv run python -m appway_backend.reporting.manual_report --no-tests

# Send to specific recipient(s) only (override REPORT_RECIPIENTS)
uv run python -m appway_backend.reporting.manual_report \
    --recipients me@example.com,colleague@example.com

# Custom date window (e.g. re-send a historical period)
uv run python -m appway_backend.reporting.manual_report \
    --from 2026-05-01 --to 2026-05-31 --recipients me@example.com
```

> ⚠️  `manual_report` **never** advances `state.json`.  The next Sunday cron
> will still pick up from the same `last_period_end`.

### Trigger the official weekly report immediately (test the timer)

```bash
sudo systemctl start appway-weekly-report.service
sudo journalctl -u appway-weekly-report.service -f
# OR
tail -f /var/log/appway-weekly-report.log
```

> This **does** advance `state.json` (same as the Sunday cron).

---

## How the period works

| Trigger | `period_start` | `period_end` | Advances state? |
|---|---|---|---|
| Sunday timer | `state.last_period_end` | now | ✅ Yes |
| `manual_report` (default) | `state.last_period_end` | now | ❌ No |
| `manual_report --from/--to` | `--from` | `--to` | ❌ No |

On the very first run (`state.json` not yet in S3), `period_start` defaults
to 2026-01-01 00:00 UTC (the system go-live epoch).

---

## Test-job handling

Jobs injected via `scripts/inject_job.sh` have `job_id` starting with `test-`.
These are:

- **Excluded** from the Sunday weekly report (Tables A + B always clinical-only)
- **Included** in `manual_report` by default (Table C in the PDF)
- **Removed** when you run `scripts/cleanup_test_jobs.sh`

After cleanup the test rows disappear from Table C on the next manual report
(liveness check: row is dropped if `results/<job-id>/result.dcm` is gone from S3).

---

## Changing recipients

Edit `.env`:

```bash
REPORT_RECIPIENTS=alice@x.com,bob@y.com,carol@z.com
```

Then restart the service (the `.env` is re-read on each run):

```bash
sudo systemctl daemon-reload   # only needed if the service unit changed
# .env changes take effect immediately — no restart needed for the running timer
```

For a one-off override without touching `.env`:

```bash
uv run python -m appway_backend.reporting.manual_report \
    --recipients me@example.com
```

---

## S3 artefacts layout

```
s3://appway-bridge-prod/
├── audit/
│   ├── 2026/05/22/final-20260522_<uuid>.json     ← per-job audit record
│   └── ...
├── results/
│   └── <job-id>/
│       ├── result.dcm                             ← ePDF DICOM (AppWay contract)
│       └── assets/
│           ├── result.pdf                         ← human-readable PDF report
│           └── <dicom-stem>/
│               ├── frame000.png                   ← extracted B-scan images
│               └── ...
├── failed/<job-id>/error.txt                      ← failure artefact (ops visibility)
├── reports/
│   ├── state.json                                 ← last_period_end + history
│   ├── 2026-05-28/
│   │   ├── report.pdf                             ← official weekly digest PDF
│   │   └── images.zip                             ← image bundle (7-day presigned URL)
│   └── ...
```

`results/<job-id>/assets/` is written by the worker at step 14b immediately
after the clinical `result.dcm` upload.  The weekly digest streams assets
**directly from S3** — the EC2 local `outputs/` directory is not involved.

---

## Install the nightly prune timer (one-time setup on a new machine)

```bash
# 1. Copy unit files
sudo cp /home/ubuntu/appway-backend/systemd/appway-prune-outputs.service \
        /home/ubuntu/appway-backend/systemd/appway-prune-outputs.timer \
        /etc/systemd/system/

# 2. Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now appway-prune-outputs.timer

# 3. Verify
systemctl list-timers appway-prune-outputs.timer
```

The prune script lives in the repo at `scripts/appway-prune-outputs.sh` — that is the
single source of truth.  The service unit's `ExecStart` points at that path so no
separate `/usr/local/sbin/` copy is needed on a fresh deploy.

---

## Local outputs/ directory and the nightly prune

`outputs/<job-id>/` on the EC2 instance is a **local operator-inspection copy**
written by the worker for short-term debugging.  It is pruned automatically:

| Timer | Schedule | Retention |
|---|---|---|
| `appway-prune-outputs.timer` | Nightly 03:00 UTC | 3 days |

```bash
# Prune status
systemctl list-timers appway-prune-outputs.timer

# Force a prune right now
sudo systemctl start appway-prune-outputs.service
cat /var/log/appway-prune.log
```

The prune script is `/usr/local/sbin/appway-prune-outputs.sh` — it logs what
it deletes with size in MB so you can track disk usage.

> The weekly digest is **not affected** by pruning — it reads from S3, not from `outputs/`.

---

## Troubleshooting

### "No recipients configured — skipping email send"
`REPORT_RECIPIENTS` is empty in `.env`.  Set it and re-run.

### SES: "Email address is not verified"
Either the recipient is not verified (SES sandbox mode) or the `REPORT_FROM`
address is not a verified SES identity.  Check:
```bash
aws ses list-identities --region eu-west-1
aws ses get-identity-verification-attributes \
    --identities darkfenner69@gmail.com --region eu-west-1
```

### "No existing state.json — starting fresh"
This is normal on first run.  The period will cover from 2026-01-01 → now.

### Weekly report shows 0 analyses
Either no jobs ran in the window, or the audit records weren't written
(check worker logs for `Audit record written`).  You can verify:
```bash
aws s3 ls s3://appway-bridge-prod/audit/ --recursive
```

### Re-send a past week's report
```bash
uv run python -m appway_backend.reporting.manual_report \
    --from 2026-05-22 --to 2026-05-28 \
    --recipients me@example.com
```

### Force-inspect what the next Sunday report will look like
```bash
uv run python -m appway_backend.reporting.manual_report \
    --no-tests --dry-run
```
This shows exactly what Tables A + B would contain without sending anything
or touching state.
