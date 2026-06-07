# AppWay Setup Notes

## Official Functional Diagram
age
The end-to-end workflow follows the official Heidelberg AppWay Functional Diagram (see `main.jpeg` in this repo). There are three zones:

### Zone 1 — Customer Side: HEYEX 2 / HEYEX PACS

- Role: **Image and Data Management** on the clinic's imaging system.
- The clinician:
  - Chooses data for analysis
  - Manages access to solution providers
  - Reviews results coming back
- Before anything leaves the clinic, HEYEX:
  - **Pseudonymizes** the DICOM data (strips real patient identifiers)
  - Encrypts it with **end-to-end encryption** (public/private key)
  - Sends it via **HTTPS** to the Heidelberg AppWay Cloud Exchange

### Zone 2 — Transit: Heidelberg AppWay / Cloud Exchange

- Role: internet relay between customer and solution provider.
- The payload is already encrypted and pseudonymized — Cloud Exchange never sees cleartext DICOM.
- Handles routing in both directions (customer → solution provider, and back).

### Zone 3 — Solution Provider Infrastructure (AWS, Azure, …)

This is our side. It contains two components, both shown as red "Heidelberg AppWay Link" boxes in the diagram:

1. **Heidelberg AppWay Link (inbound side)** — Windows EC2
   - Receives the encrypted payload from Cloud Exchange
   - **Decrypts** it using the private key
   - Writes **decrypted DICOM files into a folder** (`D:\AISolutionFolder\final-<job-id>`)

2. **Solution Provider (backend pipeline)** — Linux EC2
   - **Backend Pipeline — Performs Analysis**
   - Reads the decrypted DICOM files
   - Produces **DICOM out (ePDF report)** = `result.dcm`

3. **Heidelberg AppWay Link (outbound side)** — same Windows EC2, return path
   - Takes the DICOM result files from the folder
   - **Encrypts** them and sends them via **HTTPS + end-to-end encryption** back through Cloud Exchange
   - "Handled by Heidelberg AppWay Link" — we do not touch crypto

### Return path

- Cloud Exchange delivers the encrypted result back to HEYEX 2 / HEYEX PACS.
- HEYEX then:
  - **Receives** and **decrypts** the payload
  - **Depseudonymizes** (re-links to the real patient identity)
  - Presents the result to the clinician

### What this means for our responsibilities

| Concern | Handled by |
|---------|-----------|
| Encryption / decryption | Heidelberg AppWay Link (Windows EC2) |
| Pseudonymization / depseudonymization | HEYEX 2 / HEYEX PACS (customer side) |
| DICOM analysis and ePDF result generation | Our backend worker (Linux EC2) |
| Transport between customer and our AWS | Heidelberg AppWay Cloud Exchange |

Our backend worker sits entirely inside the **Solution Provider** circle of the diagram and only ever sees **plain, decrypted, pseudonymized DICOM files** placed in `D:\AISolutionFolder\final-*` by the inbound AppWay Link, and writes back **plain DICOM** result files for the outbound AppWay Link to encrypt.

## Local Repo

- RDP launcher script: `scripts/connect-appway-rdp.sh`
- VS Code task: `.vscode/tasks.json`
- Local notes: `README.md`
- This recap: `docs/appway.md`
- Backend worker notes: `docs/backend.md`
- Windows EC2 reference / inspection notes: `docs/appway-windows-ec2.md`
- Mermaid end-to-end sequence diagram: `docs/workflow.md` (rendered to `docs/workflow.png`)
- Consolidated roadmap of open work: `docs/next-steps.md`
- SSM helper script (run PowerShell against the Windows EC2 from the Linux side): `scripts/ssm_run.py`
- Relay package: `appway_bridge/`
- Relay entrypoints:
  - `appway_bridge/publisher.py`
  - `appway_bridge/result_consumer.py`
- Relay dependency file: `requirements.txt`

## Remote Windows EC2

- Instance ID: `i-02a99abeba370f0a7`
- Public IP / Elastic IP: `52.18.26.234`
- OS: `Windows Server 2019`

## Recovery Image

- AMI created from the configured instance:
  - `appway-win2019-golden-2026-04-19`
- AMI status at creation time:
  - `available`

This AMI can be used to launch a replacement AppWay EC2 instance without repeating the full Windows/AppWay/bridge setup from scratch.

After launching a replacement instance from this AMI, still verify:

- IAM role attachment
- security group attachment
- Elastic IP association
- scheduled tasks
- S3/SQS access

### AMI Restore Checklist

To restore the AppWay machine from the AMI:

1. Launch a new EC2 instance from:
   - `appway-win2019-golden-2026-04-19`
2. Use the intended VPC and subnet.
3. Attach the expected security group(s).
4. Attach the intended IAM role:
   - `EC2AppWayBridgeRole`
5. Confirm both EBS volumes are present:
   - system/root volume
   - data volume
6. If this replacement should take over the same public endpoint, associate the Elastic IP:
   - `52.18.26.234`
7. Connect to the new instance and verify AppWay services:
   - `MedicalCommunications AI Solution Service`
   - `MedicalCommunications Data Exchange Collector`
   - `MedicalCommunications DICOM Distributor`
8. Verify the AppWay registry configuration:
   - `HKLM:\SOFTWARE\Wow6432Node\MedicalCommunications\AISolution`
   - `AISolutionFolder = D:\AISolutionFolder`
9. Verify scheduled tasks exist and run:
   - `AppWayBridgePublisher`
   - `AppWayBridgeResultConsumer`
   - `AppWay-AISolutionFolder-Watcher` (redeploy from `scripts/appway-windows/install_ai_solution_watcher.ps1` if missing)
10. Verify relay logs are being updated:
    - `C:\AppWayBridge\logs\publisher.log`
    - `C:\AppWayBridge\logs\result_consumer.log`
11. Verify AWS access from the instance:
    - S3 bucket `appway-bridge-prod`
    - SQS queues `appway-jobs` and `appway-results`
12. Run a small smoke test if needed by placing a test `final-*` folder and checking that:
    - it is uploaded to `incoming/`
    - a message lands in `appway-jobs`
    - a test result can come back into `result-*`

If all of the above pass, the restored instance is functionally equivalent to the original AppWay machine.

## AppWay Installation

- Installer staging folder on remote machine:
  - `C:\Installers\AppWay\HDAppWayLink`
- Application path:
  - `C:\HDAppWayLink`
- AI solution path:
  - `D:\AISolutionFolder`
- Archive folder for relay:
  - `D:\AISolutionFolderArchive`

## Remote Disk Layout

- System disk:
  - `C:`
- Data disk:
  - `D:`
- `D:` is formatted as `NTFS`

## AppWay Services

Installed and running as automatic services:

- `MedicalCommunications AI Solution Service`
- `MedicalCommunications Data Exchange Collector`
- `MedicalCommunications DICOM Distributor`

## Registry

Relevant registry path:

- `HKLM:\SOFTWARE\Wow6432Node\MedicalCommunications\AISolution`

Values (verified 2026-04-25):

- `AISolutionFolder = D:\AISolutionFolder`
- `MaxAISolutionFolderProcessTimeInMin = 10`
- `MinAgeBeforeHandleResultFolderInSeconds = 1`
- `TimeBetweenRetriesInSec = 30`
- `ServiceAISolutionAutomaticCheckSleepTimeInMinutes = 20`
- `ServiceAISolutionProgramm = MCAISolution.exe`

`MaxAISolutionFolderProcessTimeInMin` is the AppWay-internal timeout: if a `final-<job-id>`
folder is not consumed within this window, AppWay moves it to its own
`D:\AISolutionFolder\failed\` subfolder. This is **distinct from our
`s3://appway-bridge-prod/failed/` operator-artifact prefix** — they serve different
purposes and must not be confused.

## Partner Registration

- Partner ID was generated during installation.
- That Partner ID must be sent to the Heidelberg / MedicalCommunications contact.

## AWS Resources

### S3

- Bucket:
  - `appway-bridge-prod`

Expected prefixes:

- `incoming/`
- `processed/`
- `results/`
- `failed/`

### SQS

Main queues:

- `appway-jobs`
- `appway-results`

Dead-letter queues:

- `appway-jobs-dlq`
- `appway-results-dlq`

### CloudWatch Alarms

Both DLQs have a symmetric CloudWatch alarm that fires when the queue depth is
≥ 1 (evaluation period 5 min, `TreatMissingData=missing`). Both alarms' action is
SNS topic `appway-dlq-alerts` which is subscribed to the operator email.

- `appway-jobs-dlq-alarm` — fires if the backend worker repeatedly fails to
  process `appway-jobs` messages (infrastructure-level failure).
- `appway-results-dlq-alarm` — fires if the `result_consumer` relay on the
  Windows EC2 repeatedly fails to process `appway-results` messages.

In addition to those two SQS-based alarms, an in-instance health-check
(**A3**, see *Relay Health Check* section below) publishes directly to
the same `appway-dlq-alerts` SNS topic if either relay scheduled task is
not in the `Running` state.

### IAM

EC2 instance role:

- `EC2AppWayBridgeRole`

Attached custom policy:

- `AppWayBridgeAccessPolicy`

Additional managed policy:

- `AmazonSSMManagedInstanceCore`

Inline policy:

- `AppWayDlqAlertsPublish` — grants `sns:Publish` on
  `arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts` so the
  `AppWayHealthCheck` scheduled task can alert operators when a relay
  stops running (see *Relay Health Check* below).

This role currently allows:

- access to the AppWay S3 bucket
- access to `appway-jobs`
- access to `appway-results`
- `sns:Publish` on `appway-dlq-alerts` (for relay health alerts)
- CloudWatch Logs write
- Systems Manager registration and remote command execution

### Systems Manager

The AppWay Windows instance is now SSM-managed:

- Instance ID:
  - `i-02a99abeba370f0a7`
- SSM agent service:
  - `AmazonSSMAgent`
- SSM status verified:
  - `Online`

This allows remote command execution against the Windows instance without opening WinRM/SSH, and is now used by the sample-ingest helper script.

## Remote Python Setup

Installed Python location:

- `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`

Virtual environment:

- `C:\Tools\appway-bridge-venv`

Working folders:

- `C:\AppWayBridge`
- `C:\AppWayBridge\logs`
- `C:\AppWayBridge\state`

Config and code on remote machine:

- `C:\AppWayBridge\config.json`
- `C:\AppWayBridge\appway_bridge\`
- `C:\AppWayBridge\requirements.txt`

## Relay Code

Local repo files:

- `appway_bridge/common.py`
- `appway_bridge/publisher.py`
- `appway_bridge/result_consumer.py`
- `requirements.txt`
- `scripts/stage-dicom-examples.sh`

Remote Windows copies:

- `C:\AppWayBridge\appway_bridge\common.py`
- `C:\AppWayBridge\appway_bridge\publisher.py`
- `C:\AppWayBridge\appway_bridge\result_consumer.py`
- `C:\AppWayBridge\appway_bridge\__init__.py`
- `C:\AppWayBridge\requirements.txt`
- Launcher scripts:
  - `C:\AppWayBridge\bin\run-publisher.ps1`
  - `C:\AppWayBridge\bin\run-result-consumer.ps1`
  - `C:\AppWayBridge\bin\cleanup-archive.ps1`

## Relay Config

Current relay config on the Windows machine:

- `region = eu-west-1`
- `bucket = appway-bridge-prod`
- `jobs_queue_url = https://sqs.eu-west-1.amazonaws.com/911167932273/appway-jobs`
- `results_queue_url = https://sqs.eu-west-1.amazonaws.com/911167932273/appway-results`
- `ai_solution_folder = D:\AISolutionFolder`
- `archive_folder = D:\AISolutionFolderArchive`
- `state_file = C:\AppWayBridge\state\state.json`
- `log_dir = C:\AppWayBridge\logs`

## Intended Relay Architecture

### Publisher on AppWay EC2

Watches:

- `D:\AISolutionFolder\final-*`

Then:

1. waits for folder stability
2. uploads contents to `s3://appway-bridge-prod/incoming/<job-id>/`
3. sends message to `appway-jobs`
4. archives local input folder

### Backend Worker

1. consumes `appway-jobs`
2. downloads input from S3
3. processes payload
4. uploads output to `s3://appway-bridge-prod/results/<job-id>/`
5. sends message to `appway-results`

### Result Consumer on AppWay EC2

1. consumes `appway-results`
2. downloads outputs from S3
3. writes temp folder in `D:\AISolutionFolder`
4. renames temp folder to `result-*`

## Flow Explanation

Per the official functional diagram, the full journey of a job is:

1. **HEYEX 2 / HEYEX PACS** (customer side) pseudonymizes the DICOM payload, encrypts it end-to-end, and sends it over HTTPS to the **Heidelberg AppWay / Cloud Exchange**.
2. The **Cloud Exchange** routes the encrypted payload over the internet to the **Heidelberg AppWay Link** running on our Windows EC2.
3. **AppWay Link decrypts** the payload and writes the cleartext DICOM files into `D:\AISolutionFolder` as folders whose names start with `final-`.
4. Our **publisher relay** (`publisher.py`) on the Windows EC2 waits until such a folder has stopped changing, uploads its files to `s3://appway-bridge-prod/incoming/<job-id>/`, sends a job message to the `appway-jobs` SQS queue, and archives the local folder into `D:\AISolutionFolderArchive`.
5. Our **backend worker** (Linux EC2) consumes `appway-jobs`, downloads the files from S3, processes them, uploads the output (a single `result.dcm` ePDF) to `s3://appway-bridge-prod/results/<job-id>/`, and sends a completion message to `appway-results`.
6. Our **result consumer relay** (`result_consumer.py`) on the Windows EC2 downloads the result payload, writes it into a temporary folder under `D:\AISolutionFolder`, and renames it to `result-<job-id>` so AppWay Link picks it up as a completed result.
7. **AppWay Link encrypts** the result and sends it back over HTTPS through the **Cloud Exchange** to HEYEX.
8. **HEYEX** decrypts the result, **depseudonymizes** it (restores real patient identifiers), and presents it to the clinician.

Our two relay processes plus the backend worker are the only pieces we own. Everything related to encryption, decryption, pseudonymization, and HTTPS transport is handled for us by AppWay Link on the Windows side and by HEYEX on the customer side.


## Verified So Far

- AppWay is installed and the three MedicalCommunications services are running.
- The registry points `AISolutionFolder` to `D:\AISolutionFolder`.
- The Windows EC2 role `EC2AppWayBridgeRole` can access the AppWay S3 bucket and both SQS queues.
- The Windows EC2 instance is SSM-managed and can receive `send-command` requests.
- `publisher.py` starts correctly and successfully published the test folder `final-test-job`.
- The test payload was uploaded to `s3://appway-bridge-prod/incoming/final-test-job/`.
- The test job was archived to `D:\AISolutionFolderArchive\final-test-job`.
- `result_consumer.py` starts correctly and successfully recreated `D:\AISolutionFolder\result-final-test-job` from S3.
- `scripts/stage-dicom-examples.sh` now works end-to-end:
  - uploads local DICOM samples to `s3://appway-bridge-prod/manual-inject/...`
  - uses SSM to copy them into `D:\AISolutionFolder\final-*`
  - triggers the publisher automatically
- The backend worker successfully processed the injected DICOM sample set `final-dicom-examples-20260419-191800`.
- The AppWay result consumer recreated:
  - `D:\AISolutionFolder\result-final-dicom-examples-20260419-191800`
- Scheduled tasks were created and started:
- Scheduled tasks were created, updated to run hidden in the background, and started:
  - `AppWayBridgePublisher`
  - `AppWayBridgeResultConsumer`
- Relay tasks hardened against external termination (2026-05-23):
  - `RestartCount = 10`, `RestartInterval = PT1M` applied to both `AppWayBridgePublisher` and
    `AppWayBridgeResultConsumer` so Task Scheduler automatically restarts them within 1 minute
    if the process is killed (e.g. by Windows Update / maintenance). Applied via SSM
    (`Set-ScheduledTask -Settings`).
- AI Solution Service watcher deployed (2026-05-18):
  - `AppWay-AISolutionFolder-Watcher` — polls `D:\AISolutionFolder` every 2 s for new `result-*` folders;
    restarts `MedicalCommunications AI Solution Service` when one appears. Original workaround for the
    service overwriting `ServiceAISolutionAutomaticCheckSleepTimeInMinutes` back to 20 on startup.
    Source: `scripts/appway-windows/install_ai_solution_watcher.ps1`.
    Reduces Δ_appway (stage [7]→[8]) from ~20 min to ~29 s.
- **Root cause of the registry overwrite identified (2026-05-26):**
  - `MCAISolutionService.exe` v1.2.2031.0 checks the registry value on startup and, **if the type is
    `REG_DWORD`**, re-stamps it to `DWORD=20`. If the type is **`REG_SZ`** (string), the code path
    skips the write entirely.
  - Confirmed by two controlled experiments via SSM on `i-02a99abeba370f0a7`:
    - **Exp A** — value `REG_SZ "1"`: Stop-Service → Start-Service → value still `REG_SZ "1"` ✅
    - **Exp B** — value `REG_DWORD 0x1`: Stop-Service → Start-Service → value overwritten to `REG_SZ "20"` ✅
  - **Permanent workaround (no watcher needed for this):** keep the value as `REG_SZ "1"`.
    Regedit GUI creates `REG_SZ` when you type a value into a manually created String Value entry —
    this is what Klaus Heidelberg's manual edit inadvertently produced, and it survives indefinitely.
  - The `AppWay-AISolutionFolder-Watcher` is kept running as a safety net (it still accelerates
    AppWay polling to 1 s from whatever value the service reads), but it is no longer the *only*
    mechanism preventing 20-minute delays.
  - Current state on the instance: `REG_SZ "1"`, service `Running` (verified 2026-05-26 08:11 UTC).
- Archive cleanup task created:
  - `AppWayArchiveCleanup`
- Archive cleanup policy:
  - delete folders in `D:\AISolutionFolderArchive` older than 7 days
- Archive cleanup schedule:
  - daily at `03:00`
- Archive cleanup missed-run behavior:
  - run as soon as possible after a scheduled start is missed
- Archive cleanup runtime:
  - `SYSTEM`
  - hidden
  - highest privileges
- Runtime log files are written to:
  - `C:\AppWayBridge\logs\publisher.log`
  - `C:\AppWayBridge\logs\result_consumer.log`
- Background execution was verified after reconfiguring the tasks so they no longer open visible PowerShell windows.
- `result_consumer.log` shows continuous polling in the background.
- `publisher.log` shows successful startup in the background and logs work only when new input folders appear.

## Remote Inspection — 2026-04-25

A live inspection of `i-02a99abeba370f0a7` via SSM (`scripts/ssm_run.py`) confirmed
that everything on the AppWay side is in place and healthy:

- SSM agent `Online`, IAM role `EC2AppWayBridgeRole` attached.
- Disks: `C:` system (≈75 GB free), `D:` NTFS data (≈200 GB free).
- `D:\AISolutionFolder` present and controlled by AppWay Link.
- `D:\AISolutionFolderArchive` present and used by the publisher relay.
- AppWay Link binary tree at `C:\HDAppWayLink`, all three MedicalCommunications
  services `Running` / `Automatic`.
- Bridge relay project at `C:\AppWayBridge\` (venv `C:\Tools\appway-bridge-venv`).
- Scheduled tasks:
  - `AppWayBridgePublisher` — `Running`
  - `AppWayBridgeResultConsumer` — `Running`
  - `AppWayArchiveCleanup` — `Ready` (daily 03:00)
  - `AppWayHealthCheck` — `Ready` (every 5 min; publishes SNS alert if a
    relay task is not `Running`; see *Relay Health Check*)
- Log sanity:
  - `publisher.log` — last startup line then idle (no pending `final-*`) ✅
  - `result_consumer.log` — continuous `No result messages available` every ~20 s ✅
- `state.json` records five successful round-tripped jobs from earlier validations.

The full walkthrough of the remote machine and the SSM reachability setup is kept in
`docs/appway-windows-ec2.md` so it does not have to be rediscovered next time.

## Status of Operational Steps

| # | Item | Status |
|---|------|--------|
| 1 | Backend worker consuming `appway-jobs` and publishing to `appway-results` | ✅ Implemented and running as systemd service on the Linux EC2 (see `docs/backend.md`) |
| 2 | Spec §9.2 error forwarding (error ePDF + `failed/<id>/` + SNS alert) | ✅ Implemented |
| 3 | Two scheduled tasks restart after Windows reboot | ✅ Validated 2026-04-25 (see "Cold-Reboot Validation" below) |
| 4 | CloudWatch alarm `appway-results-dlq-alarm` | ✅ Created 2026-04-25 (see CloudWatch Alarms above) |
| 5 | Relay-process death alerting (A3) | ✅ Implemented 2026-04-25 as `AppWayHealthCheck` scheduled task (see "Relay Health Check" below) |
| 6 | End-to-end validation with a real AppWay-originated job (HEYEX → result visible to clinician) | ⏳ Pending — requires a live job from the customer side |
| 7 | Relay task auto-restart hardening (RestartCount=10, RestartInterval=1min) | ✅ Applied 2026-05-23 via SSM — protects against external termination (Windows Update etc.) |

## Relay Health Check — 2026-04-25 (A3)

`A3` from the roadmap — operator-visible alerting when a relay scheduled task
stops running — is implemented as a small PowerShell scheduled task on the
AppWay EC2 that self-monitors the relay tasks and publishes to SNS.

### Components

- Script: `C:\AppWayBridge\bin\healthcheck.ps1` (source kept in this repo at
  `scripts/appway-windows/healthcheck.ps1`).
- Scheduled task: `AppWayHealthCheck`
  - Principal: `SYSTEM` (ServiceAccount, Highest)
  - Triggers: at startup **and** every 5 minutes (repetition duration 3650 d)
  - Action: `powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\AppWayBridge\bin\healthcheck.ps1`
  - Hardening: `-ExecutionTimeLimit 2 min`, `-MultipleInstances IgnoreNew`,
    `-StartWhenAvailable`
- Own log: `C:\AppWayBridge\logs\healthcheck.log` — one line per run,
  format `<ISO-8601 timestamp>  OK all relay tasks Running` or
  `<ISO-8601 timestamp>  ALERT sent  failedTasks=...  sns=OK`.
- IAM: new inline policy `AppWayDlqAlertsPublish` on `EC2AppWayBridgeRole`
  granting `sns:Publish` on `appway-dlq-alerts`.

### Logic

For every run, `healthcheck.ps1` calls `Get-ScheduledTask` on each of
`AppWayBridgePublisher` and `AppWayBridgeResultConsumer`. If either is not
in `Running` state (or returns an error) the script builds an operator
email body (host, instance id, timestamp, per-task `State` / `LastRunTime`
/ `LastTaskResult`, next-step hints) and publishes it to
`arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts` via the AWS CLI
installed on the instance.

No alert is sent while both relays are `Running`; each run just appends an
`OK` line to the log so operators can confirm the check itself is alive.

### Verification — 2026-04-25

- Manual run with both relays healthy → logged `OK  all relay tasks Running`, no SNS publish.
- Stopped `AppWayBridgePublisher` → manual run logged
  `ALERT sent  failedTasks=AppWayBridgePublisher  sns=OK`; SNS `MessageId`
  returned, operator email delivered.
- Restarted `AppWayBridgePublisher` → subsequent runs returned to `OK`.
- Scheduled task registered with next repetition 5 min, successfully kicked
  off by systemd-equivalent Task Scheduler boot trigger.

---

## Cold-Reboot Validation — 2026-04-25

Performed via SSM (`Restart-Computer -Force`). Result:

- Pre-reboot `LastBootUpTime`: `2026-04-25 06:53:40`
- Post-reboot `LastBootUpTime`: `2026-04-25 09:02:28`
- Post-reboot `AppWayBridgePublisher` state: `Running` ✅
- Post-reboot `AppWayBridgeResultConsumer` state: `Running` ✅
- Post-reboot `MedicalCommunications *` services: all `Running` ✅
- Fresh log activity:
  - `publisher.log` — `[publisher] Publisher started for D:\AISolutionFolder` at `2026-04-25 09:02:46` (18 s after boot)
  - `result_consumer.log` — resumed `No result messages available` every ~20 s from `2026-04-25 09:02:46`

The two scheduled tasks' boot-time triggers are confirmed working with zero
manual intervention.

---

## Relay Task Hardening + Incident — 2026-05-23

### Incident summary

At `2026-05-23 ~09:20 UTC` the `AppWayHealthCheck` task fired 7 consecutive SNS alerts
reporting both `AppWayBridgePublisher` and `AppWayBridgeResultConsumer` in `Ready` state
(not `Running`). Alert email excerpt:

```
AppWayBridgePublisher:        state=Ready  lastRun=05/20/2026 09:20:20  lastResult=267014
AppWayBridgeResultConsumer:   state=Ready  lastRun=05/20/2026 09:20:20  lastResult=267014
```

**Root cause:** `lastResult=267014` (decimal) = `0x41306` =
`SCHED_S_TASK_TERMINATED` — the Windows Task Scheduler code for "the task run was
terminated by the user/system." Both relays died at the *exact same second*
(`09:20:20`), which is the signature of a host-wide event such as Windows Update
forcing a service restart. The `AppWay-AISolutionFolder-Watcher` was unaffected
(`Running` throughout) because it was registered later with auto-restart settings
already in place.

**Resolution:** Both tasks restarted via SSM (`Start-ScheduledTask`) and confirmed
`Running` within minutes.

**No jobs were lost:** `publisher.log` shows the last real HEYEX job was processed at
`2026-05-23 06:30` (over 3 hours before the alert), and `result_consumer.log` was
polling continuously up to `09:20` — both consistent with normal idle operation.

### Hardening applied (Option B)

To prevent a recurrence, `Set-ScheduledTask -Settings` was applied via SSM to both
relay tasks adding:

| Setting | Value | Effect |
|---------|-------|--------|
| `RestartCount` | 10 | Task Scheduler will attempt up to 10 restarts |
| `RestartInterval` | `PT1M` (1 minute) | Wait 1 minute between each restart attempt |
| `StartWhenAvailable` | `$true` | Start immediately after a missed run window |
| `MultipleInstances` | `IgnoreNew` | Prevent duplicate instances if a previous run overlaps |
| `ExecutionTimeLimit` | unlimited (`PT0S`) | Never time-out the infinite-loop relay processes |

Verified via SSM immediately after:
```
AppWayBridgePublisher        RestartCount=10  RestartInterval=PT1M  State=Running ✅
AppWayBridgeResultConsumer   RestartCount=10  RestartInterval=PT1M  State=Running ✅
```

The `AppWay-AISolutionFolder-Watcher` already had identical settings from its
original installation (`install_ai_solution_watcher.ps1`). No change required there.

## Known Gaps / Things Still Missing on the AppWay Side

Operational improvements still worth doing (none of them block the current pipeline
from functioning):

1. **Two different `failed/` locations — documentation hazard.**
   - `D:\AISolutionFolder\failed\` is created and managed by AppWay Link itself
     when its own `MaxAISolutionFolderProcessTimeInMin` timeout expires.
   - `s3://appway-bridge-prod/failed/<job-id>/` is our own operator artifact prefix,
     written by `worker._forward_error_result()` on application-level failures.
   They serve different purposes. This is now called out in the "Registry" section
   above and in `docs/appway-windows-ec2.md`.

2. **End-to-end validation with a real HEYEX job.** All tests so far have been
   either locally staged (`scripts/stage-dicom-examples.sh`) or crafted job
   messages. A real customer-originated DICOM job through HEYEX → Cloud Exchange →
   AppWay Link → backend → return path has not yet been performed.
   → Tracked as **V2** in `docs/next-steps.md`.

Everything else (services, registry, IAM, S3, SQS, DLQs with alarms, scheduled
tasks surviving reboot, bridge relay code, backend worker, error forwarding,
operator alerting) is in place.

## Next Steps

The consolidated roadmap for everything still to do on this side **and** the backend
side lives in a single file: **`docs/next-steps.md`**. Do not keep AppWay-only next
steps here — update `docs/next-steps.md` instead so we have one place to look.
