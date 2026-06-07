# Backend Worker Notes

## Goal

The backend worker consumes jobs from `appway-jobs`, downloads the DICOM input payload from S3, processes it, generates a DICOM Encapsulated PDF result report, uploads it to S3, and notifies the AppWay EC2 via `appway-results`.

## System Context

This worker is the **Solution Provider** component of the official Heidelberg AppWay Functional Diagram (see `main.jpeg` and the "Official Functional Diagram" section in `appway.md`).

The end-to-end chain is:

```
HEYEX 2 / HEYEX PACS
  │ (pseudonymize + encrypt + HTTPS)
  ▼
Heidelberg AppWay / Cloud Exchange  (internet)
  │
  ▼
Heidelberg AppWay Link (Windows EC2)
  │ decrypts → writes cleartext DICOM to D:\AISolutionFolder\final-<job-id>
  ▼
publisher.py relay  →  S3 incoming/ + SQS appway-jobs
  │
  ▼
╔══════════════════════════════════════════════╗
║   THIS BACKEND WORKER (Linux EC2)            ║
║   - downloads DICOM from S3                  ║
║   - performs analysis                        ║
║   - produces result.dcm (DICOM ePDF)         ║
║   - uploads to S3 results/ + SQS appway-results║
╚══════════════════════════════════════════════╝
  │
  ▼
result_consumer.py relay  →  D:\AISolutionFolder\result-<job-id>
  │
  ▼
Heidelberg AppWay Link (Windows EC2)
  │ encrypts + sends via HTTPS
  ▼
Heidelberg AppWay / Cloud Exchange  (internet)
  │
  ▼
HEYEX 2 / HEYEX PACS
  │ decrypt + depseudonymize → clinician reviews result
```

### What this worker is NOT responsible for

| Concern | Handled by |
|---------|-----------|
| End-to-end encryption / decryption (public/private keys, HTTPS) | Heidelberg AppWay Link (Windows EC2) |
| Pseudonymization / depseudonymization of patient identifiers | HEYEX 2 / HEYEX PACS (customer side) |
| Routing between customer and our AWS infrastructure | Heidelberg AppWay Cloud Exchange |
| DICOM folder watching, S3 upload/download, SQS relaying on the Windows side | `appway_bridge/publisher.py` and `appway_bridge/result_consumer.py` on the Windows EC2 |

### What this worker assumes about its inputs

- DICOM files arriving in `s3://appway-bridge-prod/incoming/<job-id>/` are **already decrypted** (AppWay Link did that on the Windows EC2).
- Those DICOM files are **pseudonymized** — patient identifier tags in the input may already be surrogate values. We copy them verbatim into the result.
- The mandatory private AppWay credential block (`(0011,10xx)`) lives inside the input DICOM files; we copy it verbatim into `result.dcm` per spec §8 / §5.

### What this worker guarantees about its outputs

- Produces one **plain (unencrypted) DICOM** file: `result.dcm` (DICOM Encapsulated PDF, SOPClass `1.2.840.10008.5.1.4.1.1.104.1`).
- Re-emits the AppWay credential block unchanged so AppWay Link on the return trip can route the result correctly.
- Does **not** add or remove patient identifiers beyond what was in the input — depseudonymization happens later on the HEYEX side.
- Does **not** encrypt the result — the outbound AppWay Link on the Windows EC2 handles encryption before the result leaves AWS.


## AWS Contract

### SQS Queues

| Queue | Purpose |
|-------|---------|
| `appway-jobs` | Input — receives job messages from AppWay publisher relay |
| `appway-results` | Output — receives result messages consumed by AppWay result consumer relay |
| `appway-jobs-dlq` | Dead-letter queue after repeated failures on `appway-jobs` |
| `appway-results-dlq` | Dead-letter queue for `appway-results` |

### S3 Bucket: `appway-bridge-prod`

| Prefix | Purpose |
|--------|---------|
| `incoming/<job-id>/` | Input DICOM files uploaded by AppWay publisher relay |
| `results/<job-id>/` | Result files uploaded by this backend worker (including the spec §9.2 error-ePDF `result.dcm` on failures) |
| `failed/<job-id>/` | Operator failure artifacts (`error.txt` with the full traceback) written by `worker._forward_error_result()` on application-level failures |

### Job Message Body (sent by publisher relay)

```json
{
  "job_id": "final-test-job",
  "bucket": "appway-bridge-prod",
  "input_prefix": "incoming/final-test-job/",
  "result_prefix": "results/final-test-job/",
  "source_folder": "D:\\AISolutionFolder\\final-test-job",
  "published_at": "2026-04-19T08:37:14.996660+00:00"
}
```

### Result Message Body (sent by this worker)

```json
{
  "job_id": "final-test-job",
  "result_prefix": "results/final-test-job/"
}
```

---

## Current Implementation

### Code Layout

```
appway_backend/
  config.py           — loads config from .env / environment variables
  s3_utils.py         — download_prefix(), upload_directory(), upload_failure_artifact()
  sqs_utils.py        — receive_job_message(), send_result_message(), delete_message()
  sns_utils.py        — publish_error_notification() (operator alerting)
  inference.py        — YOLO MyopicCNV+ model loader + run_inference() (singleton)
  processor.py        — orchestrates per-job processing (DICOM → PNG → inference → ePDF)
  epdf_generator.py   — DICOM ePDF wrapper; delegates PDF body to pdf_report.build_pdf
  pdf_report.py       — public API shim: re-exports build_pdf, ReportJob, InputFileInfo, PerImageResult
  report/
    __init__.py       — re-exports the public API from generator.py
    generator.py      — pixel-faithful PDF layout engine (build_pdf + P0/P_GAL/P5 coords)
    templates.py      — rebuild template PNGs from .ai (run: python -m appway_backend.report.templates)
    sample_data.py    — STATIC_JOB / MOCK_JOB sandbox presets
    preview.py        — sandbox preview runner (run: python -m appway_backend.report.preview)
  worker.py           — infinite SQS poll loop + spec §9.2 error forwarding
main.py               — entry point
models/               — cached YOLO .pt weights (gitignored)
.env                  — local config (gitignored)
workdir/              — local scratch space for in-flight jobs (gitignored, outside repo, wiped after each job)
outputs/              — per-job operator artefacts: <job-id>/{*.json, *.png, result.pdf} (gitignored, local only, never uploaded to S3)
pdf_sandbox/          — layout sandbox kept on disk for reference (gitignored); new canonical code lives in appway_backend/report/
  designer_source/    — original .ai files + .otf fonts + reference PDFs from the designer (gitignored)
```

#### PDF Report Generation

The PDF body is generated by `appway_backend.report.generator.build_pdf()` via the
pixel-faithful template-overlay approach (see `README.md` for full details).

`epdf_generator.generate_epdf_dcm()` is the production entry point:
1. Reads input DICOMs for metadata + donor tags.
2. Translates the `inference_result` dict → `ReportJob` dataclass (full PNG filenames,
   `image_path` resolved via `_find_png()` for positive frames).
3. Calls `pdf_report.build_pdf(job, tmp_path)` → reads bytes → embeds in DICOM.
4. Applies DICOM wrap (file meta, patient/study tags, Clinical Trial Module,
   credential block copy, `EncapsulatedDocument`).

To iterate on the layout without touching the live pipeline:
```bash
# Edit appway_backend/report/generator.py, then:
uv run python -m appway_backend.report.preview
# → pdf_sandbox/outputs/previews/{verdict_page,image_page,table_page}.png
```


### Configuration (`.env`)

```
AWS_REGION=eu-west-1
S3_BUCKET=appway-bridge-prod
JOBS_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/911167932273/appway-jobs
RESULTS_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/911167932273/appway-results
WORK_DIR=/home/ubuntu/appway-workdir
ERROR_TOPIC_ARN=arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts

# Clinical Trial Module (B6) — version suffix on DICOM tag (0012,0020)
# ClinicalTrialProtocolID. Bump to V2/V3/… on each model/protocol revision.
CLINICAL_TRIAL_PROTOCOL_VERSION=V1
```

### How to Run

```bash
uv sync
uv run python main.py
```

### Worker Loop (steps match sequence diagram)

1. Long-poll `appway-jobs` (20s wait)
2. Receive job message → parse `job_id` and `input_prefix`
3. Download all files from `s3://appway-bridge-prod/incoming/<job-id>/` → `workdir/<job-id>/input/`
4. Run `processor.process()`:
   - Extract DICOM metadata → `outputs/<job-id>/<stem>/metadata.json`
   - Extract pixel data → `outputs/<job-id>/<stem>/image.png` (multi-frame: `image_frame000.png`…)
     — every frame is resized to **1008 × 596 RGB** (matches YOLO training input,
     see *Training/Inference Parity* below)
   - Generate DICOM ePDF result → `workdir/<job-id>/output/result.dcm`
   - Save human-readable PDF copy → `outputs/<job-id>/result.pdf`

5. Upload `workdir/<job-id>/output/` → `s3://appway-bridge-prod/results/<job-id>/`
6. Send result message to `appway-results`
7. Delete job message from `appway-jobs`
8. Loop back to step 1

On failure, two distinct paths exist (spec §9.2 compliant — see *Failure Handling* below):

- **Application-level failure** (bad DICOM, analysis crash, unexpected exception):
  the worker generates an **error ePDF** `result.dcm` (with the error description in the PDF body),
  uploads it to `s3://appway-bridge-prod/results/<job-id>/`, writes a plain-text artifact to
  `s3://appway-bridge-prod/failed/<job-id>/error.txt`, publishes an SNS notification to
  `appway-dlq-alerts`, sends the result message on `appway-results`, and deletes the job message.
  The clinician receives the error ePDF via the normal AppWay path.
- **Infrastructure-level failure** (S3/SQS unreachable, IAM revoked, etc.):
  the SQS message is left undeleted and SQS retries automatically. After `maxReceiveCount=5`
  retries the message moves to `appway-jobs-dlq`, which triggers the CloudWatch alarm
  `appway-jobs-dlq-alarm` → SNS `appway-dlq-alerts` → operator email.

---

## Result Output: DICOM Encapsulated PDF (`result.dcm`)

The single output file sent back to AppWay is `result.dcm` — a **DICOM Encapsulated PDF** (SOPClass `1.2.840.10008.5.1.4.1.1.104.1`).

Reference: Heidelberg AppWay Interface Description V4, §9.2

### DICOM Tags

| Tag | Keyword | Value |
|-----|---------|-------|
| `(0008,0016)` | SOPClassUID | `1.2.840.10008.5.1.4.1.1.104.1` (Encapsulated PDF) |
| `(0008,0018)` | SOPInstanceUID | generated fresh per job |
| `(0008,0005)` | SpecificCharacterSet | `ISO_IR 192` |
| `(0008,0060)` | Modality | `DOC` |
| `(0008,0064)` | ConversionType | `WSD` |
| `(0008,0021)` | SeriesDate | date of processing |
| `(0008,0031)` | SeriesTime | time of processing |
| `(0008,103E)` | SeriesDescription | `MyopicCNV+ Result for Job <job-id>` |
| `(0008,0070)` | Manufacturer | `MyopicCNV+` |
| `(0018,1020)` | SoftwareVersions | Resolved at import time from `pyproject.toml` (`project.version`) via `_resolve_solution_version()` — currently `0.1.0`. |
| `(0020,000E)` | SeriesInstanceUID | generated fresh per job |
| `(0020,0011)` | SeriesNumber | `1000` |
| `(0020,0013)` | InstanceNumber | `1` |
| `(0020,4000)` | ImageComments | `Result from MyopicCNV+` |
| `(0028,0301)` | BurnedInAnnotation | `YES` |
| `(0042,0010)` | DocumentTitle | `MyopicCNV+ Result for Job <job-id>` |
| `(0042,0011)` | EncapsulatedDocument | `<PDF bytes>` |
| `(0042,0012)` | MIMETypeOfEncapsulatedDocument | `application/pdf` |
| `(0008,1155)` | ReferencedSOPInstanceUID | UIDs of all input DICOM files |
| `(0012,0010)` | ClinicalTrialSponsorName | `MyopicCNV+` *(Type 1 — mandatory per DICOM Clinical Trial Subject Module, Table C.7-2b)* |
| `(0012,0020)` | ClinicalTrialProtocolID | `MYOPICCNV-APPWAY-<version>`, e.g. `MYOPICCNV-APPWAY-V1`. The version suffix is driven by the `CLINICAL_TRIAL_PROTOCOL_VERSION` env var (default `V1`) so ops can bump it on protocol/model revisions without a code change. *(Type 1)* |
| `(0012,0021)` | ClinicalTrialProtocolName | `MyopicCNV+ Non-Clinical AI Analysis` *(Type 2)* |
| `(0012,0040)` | ClinicalTrialSubjectID | Copied from the input DICOM `PatientID`. Reused rather than minting a fresh ID so the subject is traceable back to the source study. *(Type 1C — required because we do not emit `(0012,0042)` ClinicalTrialSubjectReadingID)* |
| `(0012,0072)` | ClinicalTrialSeriesDescription | `MyopicCNV+ AI Result` (or `MyopicCNV+ AI Result (ERROR)` on the error-path ePDF) *(Type 3)* |

> **Why these tags are mandatory.** The AppWay Interface Description V4 §9.2 requires non-clinical AI solutions to conform to the DICOM standard's Clinical Trial Subject Module (Table C.7-2b) and Clinical Trial Series Module (Table C.7-5b). Heidelberg MedicalCommunications confirmed on 2026-04-29: *"Please implement just as the DICOM Standard is requesting … your analysis outcomes will potentially be used in other software as well, so please conform to the standard to avoid any issues."* — see B6 in `docs/next-steps.md`.

**Patient/Study tags** (copied from first input DICOM):
`PatientName`, `PatientID`, `PatientBirthDate`, `PatientSex`, `StudyInstanceUID`, `StudyDate`, `StudyTime`, `StudyDescription`, `AccessionNumber`, `ReferringPhysicianName`

### AppWay Credential Block (mandatory — copied verbatim from input)

Per spec §8 and §5, the private credential block must be included unchanged in all result objects:

| Tag | Attribute | Description |
|-----|-----------|-------------|
| `(0011,0010)` | Private Creator | `AI Marketplace Credential Object Specific Data Block` |
| `(0011,1011)` | ASIO ID | Origin: customer system |
| `(0011,1012)` | PHO ID | Origin: customer system |
| `(0011,1021)` | Customer Partner ID | Origin: AppWay Gateway |
| `(0011,1022)` | Solution Partner ID | Origin: AppWay Link |
| `(0011,1023)` | Job ID | Cloud job UUID |
| `(0011,1031)` | Username | Optional |
| `(0011,1032)` | Password | Optional (SHA256+Base64 hashed) |
| `(0011,1041)` | AE Title | Optional — source system AE title |

### Encapsulated PDF Content

The PDF is generated by `epdf_generator.py` using ReportLab. Layout: **A4 portrait**.

| Area | Height | Content |
|------|--------|---------|
| Top blank | 3.7 cm | Reserved for AppWay patient header (name, DOB, exam date) — **must be left blank** |
| Margins | 1 cm all sides | As per spec |
| Report title | — | **MyopicCNV+** (bold 16pt) |
| Subtitle | — | Analysis Result Report |
| Job Information section | — | Job ID, processed timestamp, software version |
| Input Files section | — | Per-file: filename, modality, study/series description, frame count |
| Processing Status section | — | ✓ Files received and validated · ✓ AI analysis complete *(or ⚠ AI analysis not available on inference failure — spec §9.2 soft-fail path)* |
| **AI Analysis Result** | — | **Verdict banner** (green **NEGATIVE** / red **POSITIVE**, patient-level). Sub-line shows `X of N images flagged Positive · Processing time: T.TTs`. |
| Per-Image Results table | — | Columns: filename, result (🟢/🔴 + label), confidence, bounding box in px. Positive rows highlighted pink; the table splits across pages as needed (`repeatRows=1`). |
| Footer | — | `Not for clinical use · X/N` (centered, 7pt) — multi-page aware via a `_NumberedCanvas` two-pass trick. |

The patient-level verdict follows the original Streamlit webapp semantics: **Positive** if at
least one image is flagged Positive, otherwise **Negative**. Implementation in
`inference._majority_vote_with_equality_check()`.

---

## Per-Job Outputs Directory (local only, not sent to AppWay)

Everything an operator might want to inspect for a given job lives in a single
directory: `/home/ubuntu/appway-backend/outputs/<job-id>/`.

Each input DICOM gets its own subdirectory (named after the DICOM file stem),
so all artefacts extracted from that file stay grouped together:

```
outputs/<job-id>/
├── <dicom-stem-1>/
│   ├── metadata.json          DICOM header tags (no pixel data)
│   └── image.png              single-frame DICOM → one PNG, OR
│       image_frame000.png …   multi-frame volume → one PNG per slice
├── <dicom-stem-2>/
│   ├── metadata.json
│   └── image.png
└── result.pdf                 single combined human-readable report for the job
                               (copy of the PDF embedded in result.dcm)
```

| Path | Description |
|------|-------------|
| `<stem>/metadata.json` | All DICOM metadata tags (excluding pixel data) for that input `.dcm` |
| `<stem>/image.png` | Single 2D image → one PNG |
| `<stem>/image_frame000.png` … | Multi-frame volume → one PNG per slice (these are the exact PNGs fed to YOLO) |
| `result.pdf` | One-per-job human-readable copy of the PDF embedded in `result.dcm` — open directly, no need to unwrap DICOM |

These files are for analysis / debugging / operator review only and are **never
uploaded to S3**. The worker does not clean them up automatically — operators
decide when to prune the directory.


Note: `~/appway-workdir/<job-id>/` is a separate, **ephemeral** scratch area
that holds the raw S3 downloads and the final `result.dcm` only until it is
uploaded; it is wiped on every successful (or application-level-failed) job.


---

## Training/Inference Parity — PNG Preprocessing

**Why this matters.** The MyopicCNV+ YOLO weight (`fine_tuned_Mar24.pt`) was
fine-tuned on **HEYEX TIFF exports at 1008 × 596 RGB** that opticians supplied
directly from their HEYEX 2 workstations. The only preprocessing applied
during training was `PIL.Image.convert("RGB")` → save as JPEG — *no resize,
no CLAHE, no contrast enhancement* (reference:
`/home/ubuntu/mcnv/src/web_pages/helpers.py` → `list_of_images()`).

In production, Heidelberg Spectralis OPT volumes arrive through AppWay as
DICOM at the hardware-native **496 × 512 greyscale** B-scan size. If we fed
those raw, YOLO's internal letterbox-resize to 640 × 640 would pad the
portrait-aspect frames very differently than the landscape-aspect training
frames — a subtle but measurable domain shift that hurts detection accuracy.

**What the worker does.** `processor._prepare_for_yolo()` is called on every
frame saved by `_dicom_to_png()` (all four pixel-array shapes: single-frame
monochrome, single-frame RGB, multi-frame volume, multi-frame RGB):

1. `img.resize((TRAIN_IMAGE_WIDTH, TRAIN_IMAGE_HEIGHT), Image.LANCZOS)` →
   1008 × 596, Lanczos filter for high-quality upscaling of dense OCT
   textures.
2. `img.convert("RGB")` → match the 3-channel training tensor layout.

The result: every `outputs/<job-id>/<stem>/*.png` on disk is **1008 × 596
RGB**, identical in shape and aspect ratio to the images the model saw
during fine-tuning. YOLO's internal letterbox to 640 × 640 then behaves
identically to training.

**Config knobs** (`appway_backend/config.py`):

| Env var | Default | Meaning |
|---------|---------|---------|
| `TRAIN_IMAGE_WIDTH` | `1008` | Target PNG width fed to YOLO |
| `TRAIN_IMAGE_HEIGHT` | `596` | Target PNG height fed to YOLO |
| `YOLO_IMGSZ` | `640` | `imgsz` passed to `model.predict()` — matches the Ultralytics default used when the weight was trained |

Override these only if the model is re-trained on a different source
resolution.

**Verified.** On `docs/examples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm`
(49-frame Spectralis OPT volume, native shape `(49, 496, 512)`), the
extracted PNGs are all `size=(1008, 596) mode=RGB`. ✓

---

## AWS Permissions Required

The backend EC2 instance role (`EC2AppWayBackendRole`) must allow:

- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on `appway-jobs`
- `sqs:SendMessage` on `appway-results`
- `s3:GetObject`, `s3:ListBucket` on `appway-bridge-prod/incoming/*`
- `s3:PutObject` on `appway-bridge-prod/results/*`
- `s3:PutObject` on `appway-bridge-prod/failed/*` (failure artifact upload)
- `sns:Publish` on `arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts` (operator alerting)
- `sns:ListTopics`, `sns:GetTopicAttributes`, `sns:ListSubscriptionsByTopic` (diagnostics)
- `cloudwatch:DescribeAlarms`, `cloudwatch:PutMetricAlarm`, `cloudwatch:PutMetricData` (DLQ alarm ops)
- optionally CloudWatch Logs write

---

## Processing Rules

- Treat SQS delivery as at-least-once.
- Key everything on `job_id` to stay idempotent.
- Never delete the job message before both the result upload and result notification succeed.
- Write result files completely before uploading.

### Robustness — B7: Idempotency Guard

Since SQS delivery is at-least-once, a duplicate job message can arrive (e.g. the worker
finished the job but the `DeleteMessage` call failed or was retried). Reprocessing would
waste time, re-upload identical `result.dcm` bytes, and re-send a result notification.

`worker._handle_job()` now, as its first step, calls `s3_utils.object_exists()` on the
expected result key `results/<job-id>/result.dcm`:

- If the object exists → re-send the `appway-results` message, delete the SQS message, skip.
- If `head_object` raises any error other than `404 / NoSuchKey / NotFound` (e.g.
  AccessDenied, network) → treat as infra failure, leave message for retry → DLQ.
- Otherwise → proceed with the normal download/process/upload path.

### Robustness — B4: Workdir Cleanup

To prevent unbounded disk growth on the EC2 data volume, `worker._cleanup_workdir()`
calls `shutil.rmtree(job_dir, ignore_errors=True)` after:

- the happy-path `Job complete ✓` log, **and**
- the spec §9.2 error-forwarding path once the error result has been successfully forwarded.

It does **not** run on infrastructure-level failure (when the message is left for retry /
DLQ) so operators can inspect the workdir contents on the next attempt.

### Robustness — B8: SQS Visibility-Timeout Heartbeat

SQS delivery is at-least-once and every message has a *visibility timeout* — once a
worker receives it, SQS keeps the message hidden for `N` seconds and then makes it
visible again to another consumer if it has not been deleted. The default on
`appway-jobs` is short (30 s). Once the real AI model replaces the stub in
`processor.process()`, a single job could easily take longer than 30 s — at which
point SQS would re-deliver the same message to a second worker, causing duplicate
processing.

**Implementation** (`worker.VisibilityHeartbeat`):

- Context-manager wrapping the download → process → upload → result-send block.
- Spawns a daemon thread that calls `sqs_utils.extend_visibility()` every
  `SQS_HEARTBEAT_INTERVAL` seconds (default **25 s**), each call resetting the
  visibility window to `SQS_HEARTBEAT_EXTENSION` seconds from *now* (default **60 s**).
- Because `INTERVAL < EXTENSION`, the message is always protected for at least
  `EXTENSION − INTERVAL = 35 s` into the future.
- Uses `threading.Event.wait(interval)` so `__exit__` can wake the thread
  immediately on success; the thread joins within 5 s.
- Heartbeat failures are logged but **never** re-raised. After three consecutive
  failures the thread exits silently; the B7 idempotency guard catches any
  re-delivery that results.
- No-op when `receipt_handle` is falsy (keeps unit tests trivial).

**Interaction with other robustness features:**

| Feature | Interaction |
|---------|-------------|
| **B7 idempotency guard** | Belt-and-braces: if the heartbeat ever fails silently, a second worker will detect `result.dcm` already present and skip. |
| **B4 workdir cleanup** | Runs outside the heartbeat `with` block, so cleanup is not interrupted. |
| **Spec §9.2 error forwarding** | The heartbeat only wraps the *try* block. If an exception is raised, the `with` block exits first (stopping the heartbeat); the error-forwarding path then runs without a lingering thread. |
| **SQS retry / DLQ** | Unchanged. If the worker crashes mid-heartbeat, the last-set visibility window still expires naturally and SQS re-delivers. |

Tunable via env vars `SQS_HEARTBEAT_INTERVAL` and `SQS_HEARTBEAT_EXTENSION`
(see `config.py`).


---

## Failure Handling

Two distinct paths per spec §9.2 (error result forwarding) — both ultimately deliver an
operator email to the `appway-dlq-alerts` SNS topic subscription, and in the application-level
case also deliver an error ePDF to the clinician.

### Application-level failure (fast path)

Triggered when processing raises an exception but AWS infrastructure is healthy
(the worker can still talk to S3, SQS and SNS).

1. Log the exception with `job_id`.
2. `epdf_generator.generate_error_epdf_dcm()` builds a DICOM ePDF whose PDF body contains
   the error description and job identity. The AppWay credential block `(0011,10xx)` is
   copied from the input DICOM so AppWay Link can route it.
3. Upload the error `result.dcm` to `s3://appway-bridge-prod/results/<job-id>/`.
4. `s3_utils.upload_failure_artifact()` writes the full traceback to
   `s3://appway-bridge-prod/failed/<job-id>/error.txt`.
5. Send the completion message on `appway-results` (so the clinician receives the error ePDF).
6. `sns_utils.publish_error_notification()` publishes to `appway-dlq-alerts` with job id,
   error type, traceback, and S3 pointer — email arrives in operator inbox immediately.
7. Delete the job message from `appway-jobs` (to prevent retry loops).

### Infrastructure-level failure (slow path, fallback)

Triggered when even error-forwarding fails (S3 unreachable, SQS unreachable, IAM revoked, etc.).

1. The job message is **not** deleted from `appway-jobs`.
2. SQS makes the message visible again after the 30s visibility timeout.
3. After `maxReceiveCount=5` retries, SQS moves the message to `appway-jobs-dlq`.
4. CloudWatch alarm `appway-jobs-dlq-alarm` fires when DLQ depth ≥ 1.
5. The alarm action notifies the `appway-dlq-alerts` SNS topic → operator email.

### Two-layer alerting summary

| Layer | Trigger | Path | Latency |
|-------|---------|------|---------|
| **Fast** | Application error | Worker → SNS → Email | ~seconds |
| **Slow** | Infrastructure error | SQS retries × 5 → DLQ → CloudWatch → SNS → Email | ~minutes |

Both layers publish to `appway-dlq-alerts`, so the operator receives a single consistent
notification channel.

---

## Service Management (systemd)

The worker runs as a systemd service that starts automatically on boot and restarts on crash.

Service file: `/etc/systemd/system/appway-worker.service`

Log file:
- `/var/log/appway-worker.log` — all worker output (stdout + stderr)

```bash
# Watch live log
tail -f /var/log/appway-worker.log

# Check service status
sudo systemctl status appway-worker

# Stop / start / restart
sudo systemctl stop appway-worker
sudo systemctl start appway-worker
sudo systemctl restart appway-worker
```

---

## Verified

- **Reboot auto-restart (V1) — 2026-04-25.** Instance `i-0cc517090209eb5d1` rebooted from the AWS console.
  System boot at 09:21 UTC → systemd brought `appway-worker.service` up at 09:21:26 UTC (PID 529),
  IAM credentials re-acquired, long-poll resumed without manual intervention. A follow-up SQS
  test message was consumed at 09:22:44 UTC and correctly short-circuited via the B7 idempotency
  guard (`Result already present … re-notifying and skipping reprocess.`). ✓
- **B4 workdir cleanup + B7 idempotency guard deployed — 2026-04-25.** Verified by resending
  an already-processed job message: worker logged `Result already present …`, re-sent the
  `appway-results` message, deleted the SQS message, and did **not** reprocess. ✓
- **B8 SQS visibility-timeout heartbeat deployed — 2026-04-25.** `VisibilityHeartbeat`
  context manager added to `worker.py`; unit-test with a mocked
  `sqs_utils.extend_visibility` confirmed the background thread fires at the configured
  interval and joins cleanly on `__exit__`. Service restarted on the backend EC2
  (`appway-worker.service` PID 3408 at 10:16:46 UTC) with no regressions. ✓
- Worker starts and authenticates via IAM role `EC2AppWayBackendRole` automatically
- End-to-end test `epdf-test-001` passed:

  - Downloaded 2 OPT DICOM files from S3 ✓
  - Extracted metadata (JSON) and pixel data (PNG + 49 volume frames) ✓
  - Generated DICOM ePDF `result.dcm` with embedded A4 PDF report ✓
  - AppWay credential block copied from input DICOM ✓
  - Uploaded `result.dcm` to `s3://appway-bridge-prod/results/epdf-test-001/` ✓
  - Sent result message to `appway-results` ✓
  - Job complete in under 4 seconds ✓
- Error-forwarding path smoke test passed:
  - `generate_error_epdf_dcm()` produces a valid DICOM ePDF with embedded error PDF ✓
  - `sns_utils.publish_error_notification()` successfully publishes to
    `arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts` (delivered email to
    `darkfenner69@gmail.com`) ✓
  - Systemd `appway-worker` restarted with `ERROR_TOPIC_ARN` configured ✓
- **Real AI inference wired (B-AI) — 2026-04-25.** End-to-end test `ai-e2e-1777118639`
  passed via the live `appway-worker` systemd service:
  - Job enqueued on `appway-jobs` with 2 input OPT DICOMs (50 frames total) ✓
  - Worker downloaded inputs, extracted 50 PNGs, ran real YOLO MyopicCNV+ inference
    (singleton loader pulled the 51 MB `.pt` weight from
    `s3://ray-bucket-ai-models/yolo-april2024/fine_tuned_Mar24.pt` on first call, cached
    on disk and in-memory for subsequent jobs) ✓
  - Patient-level verdict + per-image results rendered in the ePDF via the new verdict
    banner + table (`_render_inference_section()`) ✓
  - Total wall-clock: ~109 s (≈102 s inference on CPU), well under the SQS visibility
    window thanks to the B8 heartbeat ✓
  - `s3://appway-bridge-prod/results/ai-e2e-1777118639/result.dcm` (8 784 bytes)
    uploaded + `appway-results` message sent + job message deleted ✓
  - System dependency `libgl1` is required by `ultralytics` on bare Ubuntu — installed
    via `sudo apt-get install -y libgl1 libglib2.0-0`. Recorded here so future image
    rebuilds include it.
- **Multi-page footer page numbering (B5) — 2026-04-26.** Spec §9.3 requires `n/total`
  on multi-page reports. `epdf_generator.py` now renders via a custom
  `_NumberedCanvas` subclass that does a two-pass save: pass 1 captures all page states,
  pass 2 stamps `Not for clinical use · X/N` on each page (and draws the MyopicCNV+ logo
  banner just below the 3.7 cm AppWay-reserved top strip). Verified on job
  `test-20260426_172944` — a 6-page PDF rendered `1/6` through `6/6` correctly, with
  the AppWay header zone preserved on every page.
- **`SoftwareVersions` tag cleaned (B10) — 2026-04-26.** Dropped the hard-coded
  `SOLUTION_VERSION = "0.1.0-stub"` constant; replaced with
  `_resolve_solution_version()` which tries `importlib.metadata.version("appway-backend")`
  first and falls back to parsing `pyproject.toml` directly (necessary because `uv`
  runs the project in non-packaged mode on the backend EC2). The DICOM tag
  `(0018,1020) SoftwareVersions` and the visible "Software: MyopicCNV+ v…" line on
  page 1 of the report now both show `MyopicCNV+ v0.1.0` (no more `-stub` suffix
  leaking into HEYEX's PACS UI). Verified on job `test-20260426_174801`.
- **Private-tags developer rule (B11) — 2026-04-26.** Added a prominent
  `⚠️  DEVELOPER RULE` comment block above the `(0011,10xx)` credential-block copy
  loop in `generate_epdf_dcm()` stating that the AppWay credential group is the ONLY
  private tag group permitted on `result.dcm` per spec §9.2, and warning future
  maintainers not to add other private tags anywhere in the success-path or the
  error-path ePDF. Docs-in-code only; no runtime change.
- **Registration form submitted (B3) — 2026-04-26.** The spec §4.2 / §9.1 registration
  form (data level, modality filter, marketplace/viewer details, app icons, etc.) was
  submitted to MedicalCommunications for MyopicCNV+. Awaiting Heidelberg's
  confirmation and the wiring of the test/prod HEYEX environments — this is the
  blocker for V2 (real end-to-end validation).
- **End-to-end HEYEX validation with watcher + gallery fix — 2026-05-23.** First
  fully real test with HEYEX 2 originating the job (not locally staged). 21 test
  images submitted via HEYEX; report returned in ~1m 40s (Δ_appway = 29 s) with
  correct annotated OCT images in the gallery pages. Two bugs found and fixed in this
  session:
  1. *AppWay Link 20-min polling stall* — `MCAISolutionService.exe` (v1.2.2031.0)
     hard-codes `ServiceAISolutionAutomaticCheckSleepTimeInMinutes=20` on every
     startup, overriding any registry edit within ~5 s. Workaround: scheduled task
     `AppWay-AISolutionFolder-Watcher` (`scripts/appway-windows/install_ai_solution_watcher.ps1`)
     polls `D:\AISolutionFolder` every 2 s and restarts the AI Solution Service on a
     new `result-*` folder. Deployed and verified. Reported to Heidelberg (Klaus Edelmann).
  2. *ePDF gallery "sample image not on disk"* — Round 10 patched
     `inference_result["per_image"][i]["filename"]` from the on-disk PNG name to the
     display name (e.g. `bertipagliap012.jpeg`). `epdf_generator._build_pdf_bytes()`
     then called `_find_png(fname, png_dirs)` with the patched display name, which
     failed disk lookup because the file is `.png`. Fixed in one line
     (`png_lookup = Path(fname).stem + ".png"`) in `epdf_generator.py`. Verified.

---

## Gap Analysis vs. Heidelberg AppWay Interface Description V4

Compiled against `docs/Service Manual_Heidelberg AppWay Interface Description_EN V_4.pdf`.

### 🔴 High priority — spec compliance gaps

#### 1. Error result forwarding — ✅ **DONE** (2026-04-25)
Spec §9.2 (Error Handling):
> *"Processing errors or that the credentials could not be used or were incorrect shall be forwarded to the customer for information within the key measurement object (AI Result) as written text in the PDF part of the Key Measurement Report."*

**Implementation:**
- `epdf_generator.generate_error_epdf_dcm(job_id, input_dir, output_path, error_message)` builds a valid DICOM ePDF with the error description in the PDF body and the AppWay credential block copied from the input.
- `worker._forward_error_result()` runs the full error path in `_handle_job()`'s `except` block: generate error ePDF → upload to `results/<job-id>/` → upload `failed/<job-id>/error.txt` → send `appway-results` message → publish SNS alert → delete job message.
- Existing SQS-retry / DLQ behaviour is preserved only as a second-line fallback for infrastructure failures (cannot reach S3, SQS or SNS).
- Verified by smoke test and worker restart (see *Verified* section above).

#### 2. Upload failure artifacts to S3 — ✅ **DONE** (2026-04-25)
`s3_utils.upload_failure_artifact(job_id, error_text)` now writes
`s3://appway-bridge-prod/failed/<job-id>/error.txt` containing the full traceback.
Invoked from `worker._forward_error_result()`. Never raises — a failed upload must not block
the rest of the error path.

#### 2b. Operator alerting via SNS — ✅ **DONE** (2026-04-25)
Not strictly in the spec, but added in parallel with gap #1:
- `sns_utils.publish_error_notification()` publishes a rich message (job id, host, timestamp,
  error type, traceback, S3 pointer) to the configured topic.
- Two-layer alerting: (a) direct publish from the worker's `except` block for fast application
  alerts, (b) CloudWatch alarm `appway-jobs-dlq-alarm` on DLQ depth for infrastructure failures.
  Both paths publish to the same SNS topic `appway-dlq-alerts` subscribed by operator email.

### ℹ️ Not applicable

#### Public / private keys — nothing to do
Spec §4.1:
> *"It includes a tool to create the private and public keys for the solution, because the private key will only be accessible and stored in the Heidelberg AppWay Link system."*

Key pair generation and storage is fully owned by AppWay Link on the Windows EC2.
The backend never sees keys, never encrypts, never decrypts. No action required
from our side.

#### `failed-` folder on the Windows side — AppWay-owned
Spec §4.1 defines a `failed-<job-id>` subfolder that AppWay Link itself creates on
`D:\AISolutionFolder` for DICOM files it could not deliver. This is AppWay Link's
own error handling, not something our backend writes — but worth knowing so the
support team knows where to look. Distinct from our own
`s3://appway-bridge-prod/failed/<job-id>/` operator-artifact prefix.

---

## Next Steps

The consolidated roadmap for everything still to do (backend worker + AppWay
Windows EC2 + end-to-end validation) lives in a single file:
**`docs/next-steps.md`**.

Do not keep backend-only next-steps or gap todo lists here — update
`docs/next-steps.md` so there is one place to look.
