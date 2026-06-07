# Next Steps (Consolidated)

Single source of truth for everything that is still **to do** across the whole AppWay
deployment (Windows EC2 side + Linux backend side). Items that are already done live in
their respective docs:

- `docs/appway.md` — AppWay Windows EC2 setup and verification log.
- `docs/backend.md` — Backend worker implementation + spec §9.2 error-forwarding.
- `docs/appway-windows-ec2.md` — Remote-inspection reference for the Windows EC2.
- `docs/workflow.md` — End-to-end mermaid sequence diagram (rendered to `docs/workflow.png`).

All open items are listed here. Update this file (and only this file) as work progresses.

---

## 1. AppWay Windows EC2 — Operational

| # | Item | Priority | Notes |
|---|------|----------|-------|
| A4 | **Optional — migrate scheduled tasks to a Windows Service.** Scheduled tasks are healthy today; a proper Windows Service wrapper (e.g. NSSM) would give stricter supervision and log redirection. | 🟢 Low / optional | Defer until scheduled tasks prove insufficient. |

### ✅ Done

- **A1** Cold-reboot validation — verified 2026-04-25 (see `docs/appway.md` → *Cold-Reboot Validation*).
- **A2** `appway-results-dlq-alarm` CloudWatch alarm — created 2026-04-25 (see `docs/appway.md` → *CloudWatch Alarms*).
- **A3** Relay-process death alerting via `AppWayHealthCheck` scheduled task — implemented 2026-04-25 (see `docs/appway.md` → *Relay Health Check*).
- **B4** Workdir cleanup after each job — implemented 2026-04-25 (see `docs/backend.md` → *Robustness — B4: Workdir Cleanup*).
- **B7** Idempotency guard on `results/<job-id>/result.dcm` — implemented 2026-04-25 (see `docs/backend.md` → *Robustness — B7: Idempotency Guard*).
- **B8** SQS visibility-timeout heartbeat for long jobs — implemented 2026-04-25 (see `docs/backend.md` → *Robustness — B8: SQS Visibility-Timeout Heartbeat*).
- **B-AI** Real YOLO MyopicCNV+ inference wired into `processor.py` + `epdf_generator._build_pdf()` — implemented and end-to-end verified 2026-04-25 via the live systemd worker (job `ai-e2e-1777118639`, see `docs/backend.md` → *Verified — Real AI inference wired*).
- **B5** Multi-page report page numbering — implemented & verified 2026-04-26. `_NumberedCanvas` in `epdf_generator.py` does a two-pass render and stamps `"Not for clinical use · X/N"` on every page; verified on a 6-page output (`test-20260426_172944`) where pages rendered `1/6` through `6/6` correctly. The 3.7 cm AppWay-reserved top strip is preserved on every page.
- **B10** `SoftwareVersions` tag cleaned — 2026-04-26. `SOLUTION_VERSION` in `epdf_generator.py` is now resolved at import time from the installed `appway-backend` package metadata (`importlib.metadata.version`) rather than being hard-coded to `"0.1.0-stub"`. `pyproject.toml` already carries the clean `"0.1.0"` value that flows into the DICOM tag `(0018,1020)` and the visible "Software: MyopicCNV+ v…" line on page 1.
- **V1** Backend systemd restart after reboot — verified 2026-04-25 (see `docs/backend.md` → *Verified — Reboot auto-restart*).

---

## 2. Backend Worker (Linux EC2) — Spec & Robustness

Numbering continues the gap list from the pre-refactor `docs/backend.md` so references stay
traceable.

### 2a. Spec-compliance / documentation

_(All done — nothing open here.)_

### ✅ Done (backend spec / docs)

- **B3** Registration form submitted to MedicalCommunications — submitted 2026-04-26 (data level, modality filter, marketplace/viewer details supplied per spec §4.2 / §9.1). Awaiting confirmation from Heidelberg; no further action on our side.
- **B6** Clinical Trial Module tags added to `result.dcm` — 2026-04-29. After Heidelberg's MedicalCommunications guidance (*"Please implement just as the DICOM Standard is requesting … your analysis outcomes will potentially be used in other software as well, so please conform to the standard to avoid any issues."*) `epdf_generator.py` now writes the full **Clinical Trial Subject Module** (Table C.7-2b) and **Clinical Trial Series Module** (Table C.7-5b) on both the success-path and error-path ePDF:
  - `(0012,0010) ClinicalTrialSponsorName = "MyopicCNV+"` (Type 1)
  - `(0012,0020) ClinicalTrialProtocolID = "MYOPICCNV-APPWAY-{CLINICAL_TRIAL_PROTOCOL_VERSION}"` (Type 1) — version suffix is env-driven so ops can bump it (e.g. `V1 → V2`) on protocol/model revisions without a code change. Default `V1`. Config lives in `appway_backend/config.py` → `CLINICAL_TRIAL_PROTOCOL_VERSION`.
  - `(0012,0021) ClinicalTrialProtocolName = "MyopicCNV+ Non-Clinical AI Analysis"` (Type 2)
  - `(0012,0040) ClinicalTrialSubjectID` — reuses the input DICOM `PatientID` (Type 1C; required because we don't emit `(0012,0042)`).
  - `(0012,0072) ClinicalTrialSeriesDescription = "MyopicCNV+ AI Result"` (Type 3; `"MyopicCNV+ AI Result (ERROR)"` on the error-path ePDF).

  Verified via a local smoke test (`/tmp/b6_test_out/result_ok.dcm` + `result_err.dcm` regenerated from `docs/examples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm`) — all 5 tags round-trip through `pydicom.dcmread`. DICOM Tags table in `docs/backend.md` updated accordingly.
- **B11** "Private tags developer rule" code comment added — 2026-04-26. `epdf_generator.py` now carries a prominent `⚠️ DEVELOPER RULE` block right above the `(0011,10xx)` credential-block copy loop, stating that the credential group is the ONLY private tag group permitted on `result.dcm` per spec §9.2.

### 2b. Robustness improvements

_(All done — nothing open here.)_

### 2c. Stub replacement

_(All done — nothing open here. See `docs/backend.md` → *Verified — Real AI inference wired*.)_

### 2d. Operator observability

| # | Item | Priority | Notes |
|---|------|----------|-------|
| B-REPORT | **Weekly operator report (PDF + images archive + email).** A cron-driven Python job that every Monday at 06:00 UTC scans the previous ISO week's jobs, builds a summary PDF, archives each job's `result.pdf` + extracted PNGs to S3 under `weekly-reports/<ISO-week>/`, and emails pre-signed download links via SES to the operator. | 🟡 Medium | Full spec below. All pre-requisites (SES identity verified, IAM policy attached, SES probe email sent successfully from the EC2) are already in place as of 2026-04-26 — implementation only. |

#### B-REPORT — Full Design Specification

**Goal.** Give the operator a weekly rear-view of the service: how many DICOM jobs were processed, how many succeeded vs failed, a copy of each clinician-delivered `result.pdf`, and the exact PNG B-scans the model scored — all linked from a single email so any flagged case can be re-investigated without logging into the EC2.

**Pre-requisites — ALREADY DONE (2026-04-26).**

- SES sender `darkfenner69@gmail.com` verified in `eu-west-1`. Probe email sent from the backend EC2 on 2026-04-26 at 20:09 UTC (MessageId `0102019dcb69268f-3401f59a-5f52-448a-922e-36bbba34816b-000000`) — ✅ end-to-end working.
- Inline IAM policy `AppWayWeeklyReportAccess` attached to `EC2AppWayBackendRole` — grants `ses:SendEmail` / `ses:SendRawEmail` (restricted to `ses:FromAddress = darkfenner69@gmail.com`) and `s3:PutObject` + `s3:GetObject` + `s3:ListBucket` on `appway-bridge-prod/weekly-reports/*`. Policy JSON saved at `docs/iam-weekly-report-policy.json`.

**S3 archive layout.**

```
s3://appway-bridge-prod/weekly-reports/<ISO-week>/             e.g. 2026-W18/
  report.pdf                                                   ← weekly summary PDF
  jobs/
    <PatientID>_<job-id>/                                      ← one folder per job
      result.pdf                                               ← exact copy of what HEYEX received
      images/
        <dicom-stem-1>/                                        ← one sub-folder per DICOM in the job
          b_scan_001_z1.41mm.png
          b_scan_002_z1.52mm.png
          …
        <dicom-stem-2>/
          b_scan_001_z0.88mm.png
          …
```

- `<ISO-week>` — e.g. `2026-W18`; one folder per calendar week. Never auto-pruned — operator decides.
- `<PatientID>` — extracted from the DICOM `PatientID` tag (already surrogate/pseudonymized upstream by HEYEX; see "privacy note" below).
- `<job-id>` — the AppWay job UUID. Keeps separate visits of the same patient apart.
- `<dicom-stem>` — one sub-folder per input `.dcm` (e.g. OD + OS), preserving the per-DICOM grouping already produced by `processor.py`.

**Weekly summary PDF content** (A4 portrait, same `_NumberedCanvas` + 3.7 cm AppWay-reserved top strip as `epdf_generator.py`):

| Section | Content |
|---------|---------|
| Header | Week range · Generated timestamp · Solution name/version |
| Summary table (one row per job) | Date · PatientID · DICOM files · Frames · Verdict · Time/frame · Status (✓ OK / ⚠ Failed) |
| Failed jobs detail | For each failed job: job id, error excerpt, S3 `failed/<job-id>/error.txt` pointer |
| Totals | Total jobs · Total frames · Positive rate · Mean time/frame |
| Footer | `Not for clinical use · X/N` (multi-page aware) |

> "Mean time/frame" is per image (per B-scan PNG), computed as `total_inference_time_s / total_frames`. Useful to spot CPU/model regressions.

**Email (SES).**

- **From**: `REPORT_SES_SENDER` env var — hard-default `darkfenner69@gmail.com` (already SES-verified).
- **To**: `REPORT_RECIPIENTS` env var — comma-separated list; hard-default `darkfenner69@gmail.com`.
- **Subject**: `MyopicCNV+ Weekly Report – <ISO-week> (<N> jobs, <F> failed)`.
- **Body** (plain text):
  - Top-line summary (jobs, verdicts, failures).
  - `📄 Download report PDF` → pre-signed URL (7-day expiry) to `weekly-reports/<ISO-week>/report.pdf`.
  - For each job row: `📁 Job <PatientID>_<job-id>` → pre-signed URL to its `jobs/<…>/` prefix (optionally one URL to `result.pdf` + one per images sub-folder).
- Use `boto3.client('s3').generate_presigned_url('get_object', …, ExpiresIn=604800)` for links. For prefixes ("browse this folder"), pre-sign a `list-objects-v2` request so the recipient gets a clickable index.

**Mechanism.** Linux cron on the backend EC2. Consistent with the existing systemd-based architecture.

- Cron file `/etc/cron.d/appway-weekly-report`:
  ```
  SHELL=/bin/bash
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  0 6 * * 1 ubuntu /home/ubuntu/appway-backend/scripts/run_weekly_report.sh >> /var/log/appway-weekly-report.log 2>&1
  ```
- Wrapper script `scripts/run_weekly_report.sh`:
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd /home/ubuntu/appway-backend
  exec /home/ubuntu/.local/bin/uv run python -m appway_backend.reporter
  ```

**Code changes / new files.**

1. **`appway_backend/processor.py`** — add at the end of `process()`:
   write `outputs/<job-id>/summary.json` containing:
   ```json
   {
     "job_id": "…",
     "patient_id": "…",
     "study_date": "YYYYMMDD",
     "dicom_stems": ["stem1", "stem2"],
     "total_frames": 49,
     "verdict": "NEGATIVE",                // or POSITIVE / UNAVAILABLE
     "processing_time_s": 108.3,
     "failed": false,
     "error_message": null,
     "processed_at": "2026-04-26T19:00:00Z"
   }
   ```
   Wrap in its own try/except and log-only-warn on failure so a sidecar write bug can never block the real result flow.

2. **`appway_backend/report_generator.py`** (new) — ReportLab weekly PDF builder. Reuses `_NumberedCanvas` + 3.7 cm top-strip conventions from `epdf_generator.py` for visual consistency. Pure function: `build_weekly_pdf(summaries: list[dict], out_path: Path) -> None`.

3. **`appway_backend/reporter.py`** (new) — entry point:
   - Determine the target ISO week (default = previous week; overridable via `--week 2026-W18` for testing).
   - Iterate `outputs/*/summary.json`; keep the ones whose `processed_at` falls in the target week.
   - Build the weekly PDF via `report_generator.build_weekly_pdf()` into `./workdir/weekly-<week>/report.pdf`.
   - For each job summary, upload `outputs/<job-id>/result.pdf` + every PNG under `outputs/<job-id>/<stem>/` to the S3 layout shown above.
   - Upload the weekly `report.pdf` to `weekly-reports/<week>/report.pdf`.
   - Generate pre-signed URLs (7-day expiry) for: the weekly report, each job's `result.pdf`, and each images sub-folder listing.
   - Build plain-text email body (as specified above); call `ses:SendEmail`.
   - Log summary to stdout (captured by cron into `/var/log/appway-weekly-report.log`).

4. **`appway_backend/config.py`** — add:
   ```python
   REPORT_SES_SENDER: str = os.getenv("REPORT_SES_SENDER", "darkfenner69@gmail.com")
   REPORT_RECIPIENTS: list[str] = [
       e.strip() for e in os.getenv("REPORT_RECIPIENTS", "darkfenner69@gmail.com").split(",") if e.strip()
   ]
   REPORT_S3_PREFIX: str = os.getenv("REPORT_S3_PREFIX", "weekly-reports")
   REPORT_PRESIGN_TTL_SECONDS: int = int(os.getenv("REPORT_PRESIGN_TTL_SECONDS", "604800"))  # 7 days
   ```

5. **`scripts/run_weekly_report.sh`** (new, 4 lines, executable).

6. **`/etc/cron.d/appway-weekly-report`** (new — installed manually, not via git).

7. **`docs/backend.md`** — add a new section *Weekly Operator Report* describing the cron, S3 layout, email format, env vars, and how to run a one-off report (`uv run python -m appway_backend.reporter --week 2026-W18`).

**Privacy note.** The `PatientID` we put in S3 object keys is the **surrogate/pseudonymized** ID that HEYEX already assigned before AppWay saw the DICOM (see `docs/backend.md` → *What this worker assumes about its inputs*). It is **not** the clinician-facing patient name or DOB. Still — this archive lives in a customer-accessible S3 bucket, so if the operator wants to be extra cautious we can trivially hash the PatientID with SHA256 → 8-char hex before putting it in the key. (Add as a follow-up if required.)

**Testability.** `reporter.py` takes `--week` and `--dry-run` CLI flags. `--dry-run` builds the PDF and prints the email body to stdout without uploading or sending. First production invocation can be done by hand to verify:
```
sudo -u ubuntu /home/ubuntu/appway-backend/scripts/run_weekly_report.sh
```

---

## 3. End-to-End Validation (both sides)


| # | Item | Priority | Notes |
|---|------|----------|-------|
| V2 | **Real AppWay job end-to-end test.** Trigger from a real HEYEX instance, confirm `result-<job-id>` appears on the Windows EC2 and HEYEX delivers the `result.dcm` ePDF to the clinician. | 🔴 High | Final gate before declaring the whole pipeline production-ready. |

---

## Ordering / dependencies

Suggested order (shortest path to production-ready):

1. **V2** — real end-to-end validation with a customer HEYEX instance (blocked on Heidelberg wiring the test/prod environments and confirming the B3 registration form).
2. **B-REPORT** — weekly operator observability report (pre-requisites already in place; implementation only). Can be done in parallel with V2.
3. **A4** — optional Windows Service migration.

Keep this list as the authoritative roadmap. Once an item is done, move it from here into
the "Verified"/"Implemented" section of the corresponding source doc
(`docs/appway.md` or `docs/backend.md`) and delete the row here.
