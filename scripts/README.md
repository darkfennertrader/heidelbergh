# `scripts/` — Operator Helpers for the AppWay Backend

Three shell scripts and one Python utility that let you exercise and observe
the **MyopicCNV+ backend** end-to-end.

| Script | Purpose |
|---|---|
| [`build_test_dcm.sh`](./build_test_dcm.sh) | Build a **synthetic multi-frame OPT DICOM** from a folder of JPEG/PNG images. |
| [`inject_job.sh`](./inject_job.sh) | Push one or more `.dcm` files through the pipeline (S3 upload + SQS job + log tail). |
| [`cleanup_test_jobs.sh`](./cleanup_test_jobs.sh) | Delete every `test-*` artefact from S3, the local `outputs/` dir, and the Windows `AISolutionFolder`. |
| [`job_timeline.py`](./job_timeline.py) | Print a **cross-system timeline** for a job (HEYEX → S3 → backend → S3 → HEYEX) and append it to `logs/workflow.logs`. |

Typical flow:

```
┌─────────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ build_test_dcm.sh   │ → │ inject_job.sh    │ → │ cleanup_test_jobs.sh │
│ (JPEG/PNG → .dcm)   │   │ (run pipeline)   │   │ (wipe all test-*)    │
└─────────────────────┘   └──────────────────┘   └──────────────────────┘
         ↓ at any point
┌─────────────────────────────────────────────────────────────────────────┐
│ job_timeline.py <job-id>   →   logs/workflow.logs                       │
└─────────────────────────────────────────────────────────────────────────┘
```

All three shell scripts are already `chmod +x` and assume the project venv is at
`/home/ubuntu/appway-backend/.venv` (created via `uv sync`).

---

## 1. `build_test_dcm.sh` — turn a folder of images into one DICOM

Stacks every image in a directory into a single multi-frame **Ophthalmic
Tomography (OPT)** DICOM that is structurally identical to a real AppWay file:
it copies the metadata + AppWay credential block from the reference DICOM at
`docs/examples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm` but uses
**fresh UIDs** so it never collides with the original.

### Usage

```bash
scripts/build_test_dcm.sh \
    --input  /path/to/folder-with-jpgs-or-pngs \
    --output /path/to/output-dir
```

Produces: `<output-dir>/test_<6-random-digits>.dcm`

### Behaviour

- Images are sorted alphabetically → deterministic frame order.
- Each image is converted to grayscale (`MONOCHROME2`, 8-bit).
- The **first** image's native resolution becomes the canonical `Rows × Columns`
  for the whole DICOM. Any image whose size differs is resized with LANCZOS and
  printed with a `⚠` warning.
- Original input filenames are preserved inside the DICOM:
  - `SeriesDescription` — compact `"APPWAY TEST (<N>fr from <folder>)"`
  - `ImageComments` — full comma-separated list of source filenames

### Example

```bash
scripts/build_test_dcm.sh \
    --input  ~/samples/positive_case_A \
    --output /tmp/
# → /tmp/test_483927.dcm (42 frames, 512×496)
```

---

## 2. `inject_job.sh` — manually kick off the pipeline

Simulates what the Windows EC2 publisher relay does: uploads your `.dcm`
file(s) to S3 and sends a message on the `appway-jobs` SQS queue, then tails
the worker log until the job finishes.

### Usage

```bash
scripts/inject_job.sh --files /path/to/a.dcm[,/path/to/b.dcm,…]
```

### Optional flags

| Flag | Default | Meaning |
|---|---|---|
| `--job-id <id>` | `test-<YYYYMMDD_HHMMSS>` | Override the auto-generated job id. Keep the `test-` prefix so `cleanup_test_jobs.sh` can sweep it up later. |
| `--timeout <sec>` | `300` | How long to watch `/var/log/appway-worker.log`. |
| `--no-watch` | *off* | Enqueue and exit immediately, skip log tailing. |
| `-h`, `--help` | — | Show help. |

### What it does

1. **Uploads** each `--files` entry to `s3://appway-bridge-prod/incoming/<job-id>/`
2. **Sends** an SQS message to `appway-jobs` with the same JSON shape the
   Windows publisher uses (`job_id`, `bucket`, `input_prefix`, `result_prefix`,
   `source_folder`, `published_at`).
3. **Tails** `/var/log/appway-worker.log`, filtered by `<job-id>`, until it
   sees `Job complete ✓` / `Job failed` or the timeout fires.
4. **Summarises** the local operator artefacts dumped in
   `outputs/<job-id>/` (`metadata.json`, per-DICOM PNG frames, `result.pdf`).

### Example

```bash
# End-to-end dry run
scripts/inject_job.sh --files /tmp/test_483927.dcm

# Batch of two files, shorter timeout, no job-id override
scripts/inject_job.sh --files /tmp/a.dcm,/tmp/b.dcm --timeout 120

# Fire-and-forget
scripts/inject_job.sh --files /tmp/a.dcm --no-watch
```

### Requirements

- Project venv (`/home/ubuntu/appway-backend/.venv`) has `boto3` — created
  automatically by `uv sync`.
- EC2 IAM role has `s3:PutObject` + `sqs:SendMessage`.
- `sudo` — only needed to read `/var/log/appway-worker.log` while watching.

---

## 3. `cleanup_test_jobs.sh` — wipe every `test-*` artefact

Removes every artefact that `inject_job.sh` can produce. Production data
(`final-*` / `result-final-*`) is **never** touched — the script filters
strictly on the `test-*` prefix everywhere.

### Usage

```bash
scripts/cleanup_test_jobs.sh
```

(No flags beyond `-h` / `--help`.)

### What it deletes

| # | Location |
|---|---|
| 1 | `s3://appway-bridge-prod/incoming/test-*/` |
| 2 | `s3://appway-bridge-prod/results/test-*/` |
| 3 | `/home/ubuntu/appway-backend/outputs/test-*/` |
| 4 | Windows EC2 `D:\AISolutionFolder\result-test-*\` (via SSM → `scripts/ssm_run.py`) |

Exits `0` on a fully-clean sweep; non-zero (and aborts immediately) on the
first failure.

### Requirements

- Project venv with `boto3`.
- EC2 IAM role: `s3:ListBucket` + `s3:DeleteObject` on `appway-bridge-prod`
  and `ssm:SendCommand` / `ssm:GetCommandInvocation` for the Windows EC2
  `i-02a99abeba370f0a7`.

---

## 4. `job_timeline.py` — cross-system job timeline

Assembles a human-readable end-to-end log of **all 9 pipeline stages** for a
given job — from DICOM received on HEYEX 2 all the way to the result being
stored back in HEYEX — and appends it to `logs/workflow.logs`.

### Stages covered

| # | Stage | Data source |
|---|---|---|
| ① | DICOM received by AppWay Link (heyex2) | SSM-grep `AppWay Link\Logs\*.log` |
| ② | DICOM uploaded to S3 `incoming/` | S3 `LastModified` |
| ③ | Job enqueued on SQS appway-jobs | (derived ≈ stage ②) |
| ④ | Input downloaded by backend | `journalctl -u appway-worker` / `STAGE 4/9` |
| ⑤ | Backend processes (YOLO + ePDF) | journal `STAGE 5/9` |
| ⑥ | ePDF result uploaded to S3 `results/` | journal `STAGE 6/9` + S3 `LastModified` |
| ⑦ | Result enqueued on SQS appway-results | journal `STAGE 7/9` |
| ⑧ | Result downloaded by AppWay Link | SSM-grep AppWay Link log |
| ⑨ | Result stored into HEYEX | SSM-grep `MCAshvinsWorkstation.verbose.log` |
| ✗ | User-click failure (if any) | SSM-grep `ThreadLoadDICOMReport` error |

Each line shows:
- `[+HH:MM:SS]` elapsed from stage ①
- `YYYY-MM-DD HH:MM:SS CEST  (HH:MM:SS UTC)`  (primary CEST, secondary UTC)
- Stage label + S3 path / local path + file size

### Usage

```bash
# One-shot — query all sources once, print, append to logs/workflow.logs:
python3 scripts/job_timeline.py final-5f1e35fa-3397-4604-b5c1-a7785919ea13

# Live mode — re-query every 5 s, auto-exit when stage ⑨ appears:
python3 scripts/job_timeline.py final-5f1e35fa-… --live

# Faster live refresh:
python3 scripts/job_timeline.py final-5f1e35fa-… --live --interval 10

# UTC timestamps only (for AWS support tickets):
python3 scripts/job_timeline.py final-5f1e35fa-… --utc-only

# Skip SSM queries (backend-only view, faster):
python3 scripts/job_timeline.py final-5f1e35fa-… --no-heyex
```

### Output file

All timelines are **appended** (never overwritten) to:

```
logs/workflow.logs
```

The `logs/` directory is committed (via `logs/.gitkeep`) but `*.logs` and
`*.log` files inside it are ignored by `.gitignore` — so the log accumulates
on each operator's machine without polluting the repo.

### Requirements

- Project venv with `boto3` (`uv sync`).
- EC2 IAM role: `s3:ListBucket` + `s3:GetObject` on `appway-bridge-prod`,
  `ssm:SendCommand` + `ssm:GetCommandInvocation` for both
  `i-02a7dd1797d85a099` (heyex2) and `i-02a99abeba370f0a7` (backend).
- `journalctl` — available when running on the backend EC2; otherwise
  the script falls back to SSM automatically.

---

## Quick recipe — full end-to-end test

```bash
# 1. Build a synthetic DICOM from a folder of OCT slices
scripts/build_test_dcm.sh \
    --input  ~/samples/case_42 \
    --output /tmp/

# 2. Push it through the pipeline (watches the log)
scripts/inject_job.sh --files /tmp/test_*.dcm

# 3. Review the result
xdg-open outputs/test-*/result.pdf

# 4. When you're done, wipe every test-* artefact from all four locations
scripts/cleanup_test_jobs.sh
```
