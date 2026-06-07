# Heidelberg AppWay Full Sequence Diagram

End-to-end sequence for a single job, matching the official Heidelberg AppWay Functional Diagram (`main.jpeg`). Covers the customer system, Cloud Exchange, AppWay Link (with encryption/decryption), the bridge relays, the backend worker, and the three-layer operator alerting (direct SNS publish from the backend, CloudWatch DLQ alarms, and the AppWay-side `AppWayHealthCheck` scheduled task).

```mermaid
sequenceDiagram
    autonumber
    participant External as HEYEX 2 / HEYEX PACS<br/>(Customer System)
    participant Cloud as Heidelberg AppWay<br/>Cloud Exchange
    participant AppWay as Heidelberg AppWay Link<br/>Windows EC2
    participant AIS as D:\AISolutionFolder
    participant Publisher as Publisher Relay<br/>publisher.py
    participant S3 as S3<br/>appway-bridge-prod
    participant Jobs as SQS<br/>appway-jobs
    participant Backend as Backend Worker<br/>(Solution Provider)
    participant Results as SQS<br/>appway-results
    participant Consumer as Result Relay<br/>result_consumer.py
    participant AISvc as MedicalComm.<br/>AI Solution Service
    participant Watcher as AppWay-AISolutionFolder<br/>-Watcher (Task)
    participant HealthCheck as AppWayHealthCheck<br/>(scheduled task)
    participant DLQ as SQS<br/>appway-jobs-dlq
    participant CW as CloudWatch<br/>Alarm
    participant SNS as SNS<br/>appway-dlq-alerts
    participant Ops as Operator<br/>(email)

    External->>External: Pseudonymize DICOM + Encrypt (E2E, public/private key)
    External->>Cloud: Send encrypted payload via HTTPS
    Cloud->>AppWay: Deliver encrypted payload
    AppWay->>AppWay: Decrypt payload
    AppWay->>AIS: Create receiving-<job-id>
    AppWay->>AIS: Promote to final-<job-id>

    Publisher->>AIS: Poll for final-* folders
    Publisher->>Publisher: Wait for stable folder
    Publisher->>S3: Upload files to incoming/<job-id>/
    Publisher->>Jobs: Send job message
    Publisher->>AIS: Move final-<job-id> to archive folder

    Backend->>Jobs: Poll for next job
    Jobs-->>Backend: Deliver job message
    Backend->>S3: Download incoming/<job-id>/*

    alt Processing succeeds
        rect rgb(200, 240, 200)
            Backend->>Backend: Process payload — generate result.dcm (DICOM ePDF)
            Backend->>S3: Upload result.dcm to results/<job-id>/
            Backend->>Results: Send result message
            Backend->>Jobs: Delete job message ✓
        end

    else Application-level failure (spec §9.2 error forwarding)
        rect rgb(255, 220, 220)
            Backend->>Backend: Generate ERROR result.dcm<br/>(error message in the PDF body)
            Backend->>S3: Upload error result.dcm to results/<job-id>/
            Backend->>S3: Upload error.txt to failed/<job-id>/
            Backend->>Results: Send result message<br/>(clinician receives error ePDF)
            Backend->>SNS: Publish operator notification<br/>(direct, immediate)
            SNS-->>Ops: Email: "[AppWay] Job <id> failed"
            Backend->>Jobs: Delete job message ✓
        end

    else Infrastructure failure (S3/SQS unreachable)
        rect rgb(255, 240, 200)
            Note over Backend,Jobs: Message NOT deleted → SQS retry
            Jobs-->>DLQ: After maxReceiveCount=5 → move to DLQ
            DLQ-->>CW: ApproximateNumberOfMessagesVisible ≥ 1
            CW-->>SNS: Alarm state → IN_ALARM
            SNS-->>Ops: Email: "appway-jobs-dlq-alarm"
        end
    end

    Consumer->>Results: Poll for result message
    Results-->>Consumer: Deliver result message
    Consumer->>S3: Download results/<job-id>/*
    Consumer->>AIS: Write tmp-result-<job-id>
    Consumer->>AIS: Rename to result-<job-id>
    Consumer->>Results: Delete processed result message

    Note over AISvc: Hard-codes 20-min poll on startup<br/>(overrides registry within ~5 s)
    Watcher->>AIS: Poll every 2 s for new result-* folders
    Watcher->>AISvc: Restart-Service on new result-*
    AISvc->>AIS: Detect result-<job-id>
    AISvc->>AppWay: Hand off result to AppWay Link
    AppWay->>AppWay: Encrypt result (E2E, public/private key)
    AppWay->>Cloud: Send encrypted result via HTTPS
    Cloud->>External: Deliver encrypted result
    External->>External: Decrypt + Depseudonymize
    External->>External: Clinician reviews result in HEYEX

    Note over HealthCheck: Runs every 5 min on Windows EC2 (A3)
    HealthCheck->>Publisher: Get-ScheduledTask state
    HealthCheck->>Consumer: Get-ScheduledTask state
    HealthCheck->>Watcher: Get-ScheduledTask state
    alt Any relay task not Running
        rect rgb(255, 220, 220)
            HealthCheck->>SNS: Publish relay-down alert
            SNS-->>Ops: Email: "[AppWay] Relay health check FAILED"
        end
    end
```

## Legend

- **External** — HEYEX 2 / HEYEX PACS on the customer side; handles pseudonymization, encryption on send, decryption + depseudonymization on receive.
- **Cloud** — Heidelberg AppWay Cloud Exchange; transport-only, sees only encrypted payloads.
- **AppWay** — Heidelberg AppWay Link running on our Windows EC2; decrypts inbound payloads and encrypts outbound results. Owns `D:\AISolutionFolder`.
- **Publisher / Consumer** — our two bridge relays on the Windows EC2 (`appway_bridge/publisher.py` and `appway_bridge/result_consumer.py`).
- **Backend** — our Linux EC2 worker (the "Solution Provider" in the official diagram); performs the analysis and produces `result.dcm` (DICOM Encapsulated PDF).
- **AISvc** — `MedicalCommunications AI Solution Service` inside AppWay Link. Responsible for polling `D:\AISolutionFolder` for `result-*` folders and handing them off to the AppWay Link outbound path for encryption. Hard-codes `ServiceAISolutionAutomaticCheckSleepTimeInMinutes=20` on every startup (overrides any registry edit within ~5 s), causing a ~20-min pickup delay unless the watcher is running.
- **Watcher** — `AppWay-AISolutionFolder-Watcher` scheduled task on the Windows EC2 (source: `scripts/appway-windows/install_ai_solution_watcher.ps1`). Polls `D:\AISolutionFolder` every 2 s; when it detects a new `result-*` folder it issues `Restart-Service "MedicalCommunications AI Solution Service"`, forcing the service to pick up the result immediately. Reduces Δ_appway (stage [7]→[8]) from ~20 min to ~29 s. Monitored by `AppWayHealthCheck`.
- **HealthCheck** — `AppWayHealthCheck` scheduled task on the Windows EC2 (`scripts/appway-windows/healthcheck.ps1`, deployed to `C:\AppWayBridge\bin\healthcheck.ps1`). Runs every 5 minutes as SYSTEM, checks that `AppWayBridgePublisher`, `AppWayBridgeResultConsumer`, and `AppWay-AISolutionFolder-Watcher` are all in the `Running` state, and publishes an operator alert to SNS when any is not.
- **S3 / Jobs / Results** — AWS infrastructure used as the in-cloud handoff between the Windows side and the Linux worker.
- **DLQ** — `appway-jobs-dlq`, reserved for infrastructure failures only (not application errors). A symmetric `appway-results-dlq` protects the return leg (result consumer on the Windows EC2).
- **CW** — CloudWatch Alarms `appway-jobs-dlq-alarm` and `appway-results-dlq-alarm`: each fires when its DLQ depth ≥ 1.
- **SNS** — SNS topic `appway-dlq-alerts` subscribed by operator email; receives direct worker-side publishes (fast, application errors), CloudWatch alarm actions (slow, infrastructure errors), and `AppWayHealthCheck` publishes (relay death on the Windows EC2).
- **Ops** — on-call operator email subscription on the SNS topic.

## Three-layer alerting

| Layer | Trigger | Path | Use case |
|-------|---------|------|----------|
| **Fast (backend)** | `Backend → SNS → Ops` | Direct `sns:Publish` from worker `except` block | Application errors: bad DICOM, analysis crash, unexpected exception. Clinician still gets an error ePDF. |
| **Slow (DLQ)** | `Backend ↛ SQS → DLQ → CW → SNS → Ops` | CloudWatch alarm on DLQ depth | Infrastructure failures on the backend path: S3/SQS unreachable, IAM revoked, persistent network problems. Worker retries via SQS, then DLQ fires the alarm. Symmetric alarm on `appway-results-dlq` protects the return leg. |
| **Relay-death (AppWay side)** | `HealthCheck → SNS → Ops` | Every 5 min: `AppWayHealthCheck` scheduled task checks `AppWayBridgePublisher` and `AppWayBridgeResultConsumer` state; publishes if either ≠ `Running`. | Permanent death of a relay scheduled task (Python crash loop, configuration error, manual stop). Without this, the backend would sit idle with no failing DICOM to retry and nothing to populate the DLQ. |

All three layers publish to the same SNS topic (`appway-dlq-alerts`), so operators receive a single, consistent email notification channel.
