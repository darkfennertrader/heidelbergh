# `scripts/` — Operator Helpers for the AppWay Backend

Three shell scripts and one Python utility that let you exercise and observe
the **MyopicCNV+ backend** end-to-end.

| Script | Purpose |
|---|---|
| [`build_test_dcm.sh`](./build_test_dcm.sh) | Build a **synthetic multi-frame OPT DICOM** from a folder of JPEG/PNG images. |
| [`inject_job.sh`](./inject_job.sh) | Push one or more `.dcm` files through the pipeline (S3 upload + SQS job + log tail). |
| [`cleanup_test_jobs.sh`](./cleanup_test_jobs.sh) | Delete every `test-*` artefact from S3, the local `outputs/` dir, and the Windows `AISolutionFolder`. |
| [`job_timeline.py`](./job_timeline.py) | Print a **cross-system timeline** for a job (HEYEX → S3 → backend → S3 → HEYEX) and append it to `logs/workflow.logs`. |
| [`appway-prune-outputs.sh`](./appway-prune-outputs.sh) | **Nightly prune** — deletes `outputs/<job>/` folders older than 3 days. Called by `appway-prune-outputs.timer` (03:00 UTC). Logs to `/var/log/appway-prune.log`. |

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

Streams a human-readable end-to-end log of **all 9 pipeline stages** for a
job — from DICOM received on HEYEX 2 all the way to the result stored back
in HEYEX — writing each stage line to **`logs/workflow.logs`** immediately
as it is detected.

### Stages covered

| Tag | Stage | Data source | Notes |
|---|---|---|---|
| `[1]` | DICOM received by AppWay Link | `AshvinsDistribution\` — `.zip` file | AppWay zips before S3 upload, so this is ~30 s **before** stage `[2]` |
| `[2]` | DICOM uploaded to S3 `incoming/` | S3 `LastModified` | |
| `[3]` | Job enqueued on SQS appway-jobs | (derived ≈ stage `[2]`) | |
| `[4]` | Input downloaded by backend | `/var/log/appway-worker.log` | |
| `[5]` | Backend processes (YOLO + ePDF) | `/var/log/appway-worker.log` | |
| `[6]` | ePDF result uploaded to S3 `results/` | `/var/log/appway-worker.log` + S3 | |
| `[7]` | Result enqueued on SQS appway-results | `/var/log/appway-worker.log` | |
| `[8]` | Result stored by AppWay Link | `AshvinsDistribution\` — small `.dcm` file (<100 KB) | Result file, **not** `.rtc.dcm` — confirmed on live traffic |
| `[9]` | Result stored into HEYEX | `UVOBackup\…-AIResultBackup-<uuid>` folder | HEYEX creates this folder when it starts importing; this is when the report becomes **visible in the UI** (~1 s after `[8]`). The later `UVOJob-N-DeleteImage-Done` is a background cleanup not related to UI visibility. |
| `[X]` | User-click failure (if any) | SSM → `MCAshvinsWorkstation.verbose.log` | WebView2 "can't reach this page" error |

### Usage

#### Recommended workflow — start the watcher *before* dragging the DCM into HEYEX

```bash
# Terminal 1: start watcher (auto-detects new job, streams stages as they arrive)
python3 scripts/job_timeline.py --live

# Terminal 2 (optional): watch the log file grow in real time
tail -f logs/workflow.logs
```

The script prints:

```
  Live mode — waiting for a new job in s3://appway-bridge-prod/incoming/
  (started at 2026-05-18 10:00:00 CEST  (08:00:00 UTC) · poll every 5s · Ctrl-C to stop)
  Waiting...  3s elapsed
  New job: final-41707dc3-…  (uploaded 2026-05-18 10:01:46 CEST  (08:01:46 UTC))

  AppWay job:  final-41707dc3-b8f7-4a9e-bbcc-9ee8738adecd
  Watching at: 2026-05-18 10:01:47 CEST  (08:01:47 UTC)

  TIME (CEST)    ELAPSED    ST   STAGE
  ─────────────  ─────────  ───  ──────────────────────────────────────────
  11:32:38 CEST  +00:00:00  [1]  DICOM received by AppWay Link
                                 AshvinsDistribution/20260518113237.gxtdcyx5.igb.zip
  11:33:08 CEST  +00:00:30  [2]  DICOM uploaded to S3
                                 s3://appway-bridge-prod/incoming/…/….dcm  (457 KB)
  11:33:08 CEST  +00:00:30  [4]  Input downloaded by backend
                                 s3://appway-bridge-prod/incoming/…/  (1 file(s))
  11:33:08 CEST  +00:00:30  [5]  Backend processes (YOLO + ePDF)
  11:33:11 CEST  +00:00:33  [7]  Result enqueued on SQS appway-results
  11:33:12 CEST  +00:00:34  [6]  ePDF result uploaded to S3
  Waiting for stage [9]...  idle timeout in 4m 59s
  11:34:51 CEST  +00:02:13  [8]  Result stored by AppWay Link
                                 AshvinsDistribution/20260518113451.vukbypme.n0b.dcm
  11:35:05 CEST  +00:02:27  [9]  Result stored in HEYEX
                                 UVOBackup/…UVOJob-20-DeleteImage-Done

  Summary
  -------
  Stages seen   : [1] [2] [4] [5] [6] [7] [8] [9]
  Stages missed : [3]
  Total elapsed:  2m 27s   ([1] -> [9])
```

#### One-shot (auto-detect newest job already in S3)

```bash
python3 scripts/job_timeline.py
```

#### Explicit job-id

```bash
# One-shot:
python3 scripts/job_timeline.py final-5f1e35fa-3397-4604-b5c1-a7785919ea13

# Live:
python3 scripts/job_timeline.py final-5f1e35fa-3397-4604-b5c1-a7785919ea13 --live
```

### Flag reference

| Flag | Default | Description |
|---|---|---|
| `--live` | off | Stream events; exit on stage `[9]` or idle timeout |
| `--interval SEC` | `5` | Poll interval in seconds (live mode) |
| `--idle-timeout SEC` | `900` | Seconds of inactivity after stage `[6]` before auto-exit (15 min) |
| `--quick` | off | Shortcut for `--idle-timeout 300` (5 min); useful for dev/CI runs |
| `--utc-only` | off | Show UTC timestamps only (useful for AWS support tickets) |
| `--no-heyex` | off | Skip SSM queries to heyex2 (faster, backend-only view) |

### Realistic timing expectations

End-to-end pipeline latency observed on the production workstation (`EC2AMAZ-UIM0T5T`):

| Window | Typical | Worst observed | Notes |
|---|---|---|---|
| `[1]` → `[2]` | −30 s | −60 s | AppWay zips *before* S3 upload; `[1]` is always earlier than `[2]` |
| `[2]` → `[7]` | ~3 s | ~10 s | Cloud AI pipeline (S3 upload → YOLO inference → ePDF → S3 result → SQS) |
| `[7]` → `[8]` | 1–2 min | 4 min | AppWay Link polls S3 results and writes result `.dcm` to drop folder |
| `[8]` → `[9]` | ~1 s | ~5 s | HEYEX creates the `AIResultBackup` folder almost immediately after `[8]`; report is visible in the UI at this point |
| **`[1]` → `[9]` total** | **3–4 min** | **~15 min** | |

> **Note — stage `[3]`**: The SQS `appway-jobs` enqueue happens within 1–2 s of the S3 upload (`[2]`) and is not independently observable. The tool emits it as a synthetic stage at the `[2]` timestamp.
>
> **Note — parallel jobs**: Each watcher pairs `[9]` with its own `[8]` (floored at `[8] ts − 5 s`) so two concurrent watchers never claim the same `AIResultBackup` folder.
>
> **Note — live-mode streaming behaviour**: When a single 5-second poll surfaces multiple new stages simultaneously, they are printed with a ~150 ms pause between each line so the output trickles in sequentially rather than bursting all at once. Timestamps shown are the real event times — only the *display* is paced.
>
> **Note — stage ordering within the same second**: Several stages share a near-identical timestamp (e.g. `[2]` from S3 has millisecond precision while `[4]` from the worker log has second precision only, so they can appear to collide). Both `build_timeline` and the live loop sort stages by **2-second time buckets**, then by canonical stage number within each bucket — so the output always reads `[1] → [2] → [3] → [4] → …` regardless of sub-second measurement noise.
>
> **Note — `[8]` / `[9]` race condition**: The tool queries `AshvinsDistribution` (for `[8]`) and `UVOBackup` (for `[9]`) as two sequential SSM round-trips (~2–4 s each). In rare cases `[8]` appears in the sub-second gap between the two queries: `[9]` is detected but `[8]` is not. The watcher handles this automatically: if `[9]` is seen but `[8]` is absent it does **one final re-scan** of `AshvinsDistribution` before exiting and emits `[8]` if it is now present. A one-line notice `(re-checking AshvinsDistribution for [8]...)` is printed when this path is taken.

### Troubleshooting: stage `[8]` never arrives

If the watcher counts down past `[6]`/`[7]` but `[8]` never fires, AppWay Link is not picking up the result from S3. Symptoms in the logs:

- `MCAshvinsWorkstation.verbose.log` stops updating after a `MiiiDcmFile constructor error, couldn't open file \\<hostname>\…` stack trace
- `AshvinsDistribution\` contains only the input `.zip` (no new `.dcm`)

**Likely cause**: The WebView2 "can't reach this page" click-error puts the AppWay Link polling thread into a stuck state. The system typically self-recovers after 10–15 min, or can be unstuck by restarting `MCAshvinsWorkstation`.

**What to do**: Wait for the 15-minute idle timeout. If `[8]` still never appears, check `C:\HEYEX\logfiles\MCAshvinsWorkstation.verbose.log` for the last stack trace and file a support request with Heidelberg (team-ashvins@heidelbergengineering.com) attaching the full `logfiles\` folder.

### Output file — `logs/workflow.logs`

- Each stage line is written to `logs/workflow.logs` **immediately** when
  detected — not only when the job finishes.
- The watcher exits cleanly (with a summary + separator) on **four** paths:
  stage `[9]` seen · idle timeout · Ctrl-C · any unhandled error.
- The file is **appended**, never overwritten — safe to run multiple jobs in
  sequence without losing history.
- `logs/workflow.logs` is in `.gitignore`; the directory is tracked via
  `logs/.gitkeep`.  To start fresh: `rm logs/workflow.logs`.

### Requirements

- Project venv with `boto3` (`uv sync`).
- EC2 IAM role: `s3:ListBucket` + `s3:GetObject` on `appway-bridge-prod`,
  `ssm:SendCommand` + `ssm:GetCommandInvocation` for both
  `i-02a7dd1797d85a099` (heyex2) and `i-02a99abeba370f0a7` (backend).

---

## Quick recipe — full end-to-end test (HEYEX UI flow)

```bash
# 1. Build a fresh synthetic DICOM from example images
scripts/build_test_dcm.sh \
    --input  docs/examples/images \
    --output docs/examples
# → docs/examples/test_NNNNNN.dcm

# 2. Start the job watcher BEFORE you drag the file into HEYEX
python3 scripts/job_timeline.py --live
#    (in another terminal, optionally):
#    tail -f logs/workflow.logs

# 3. Drag docs/examples/test_NNNNNN.dcm into the HEYEX 2 UI
#    The watcher auto-detects the new job and streams each stage as it arrives.
#    It exits automatically when stage [9] lands (or after 5 min idle).

# 4. Review the produced PDF in the HEYEX UI.
#    Full timestamped record is in:
cat logs/workflow.logs | tail -50

# 5. Wipe test-* artefacts from S3 + local outputs
scripts/cleanup_test_jobs.sh
```

## Quick recipe — pipeline-only test (no HEYEX UI, inject_job.sh)

```bash
# 1. Build DICOM
scripts/build_test_dcm.sh --input docs/examples/images --output /tmp/

# 2. Push directly through S3 + SQS (no HEYEX UI required)
scripts/inject_job.sh --files /tmp/test_*.dcm

# 3. Review result
xdg-open outputs/test-*/result.pdf

# 4. Cleanup
scripts/cleanup_test_jobs.sh
```
