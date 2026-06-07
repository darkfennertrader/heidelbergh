# AppWay Windows EC2 — Access & Inspection Notes

Recovery / reference document for the Heidelberg AppWay Link Windows EC2 machine (the "Zone 3 / inbound+outbound" side of the official AppWay Functional Diagram). Use this to re-establish access from the Linux backend or any other tooling without asking the operator for the same details over and over.

## Instance

| Field | Value |
|-------|-------|
| Instance ID | `i-02a99abeba370f0a7` |
| Public / Elastic IP | `52.18.26.234` |
| Private IP | `172.31.41.145` |
| Computer name | `EC2AMAZ-5FC8OF3.WORKGROUP` |
| OS | Windows Server 2019 Datacenter (10.0.17763) |
| AMI (golden) | `appway-win2019-golden-2026-04-19` |
| Region | `eu-west-1` |
| SSM Agent | `AmazonSSMAgent` — Running — Automatic — `3.3.3797.0` |
| SSM Ping status | `Online` |
| IAM role | `EC2AppWayBridgeRole` (attached via IMDSv2) |

## SSM access (from the Linux backend EC2)

The Linux backend's IAM role (`EC2AppWayBackendRole`) has an inline policy called `AppWaySSMInspect` that grants:

```
ssm:SendCommand
ssm:GetCommandInvocation
ssm:ListCommandInvocations
ssm:DescribeInstanceInformation
ssm:StartSession
ssm:TerminateSession
ssm:DescribeSessions
ec2:DescribeInstances
ec2:DescribeInstanceStatus
```

The Windows instance's role (`EC2AppWayBridgeRole`) has the AWS-managed policy `AmazonSSMManagedInstanceCore` which lets the SSM Agent register.

### Helper script

`scripts/ssm_run.py` wraps `SendCommand` + `GetCommandInvocation`, polling until the PowerShell script finishes. Use it from this repo:

```bash
/home/ubuntu/appway-backend/.venv/bin/python \
    /home/ubuntu/appway-backend/scripts/ssm_run.py \
    'Get-Service AmazonSSMAgent | Format-List *'
```

Confirm reachability without sending a command:

```bash
/home/ubuntu/appway-backend/.venv/bin/python -c "
import boto3, json
c = boto3.client('ssm', region_name='eu-west-1')
print(json.dumps(c.describe_instance_information(
    Filters=[{'Key':'InstanceIds','Values':['i-02a99abeba370f0a7']}]
), default=str, indent=2))
"
```

## Observed layout (inspection on 2026-04-25)

### Disks
- `C:` — system disk (used 24.79 GB / free 75.21 GB)
- `D:` — data disk, NTFS (used 0.12 GB / free 199.87 GB)

### `D:\` top level
```
D:\AISolutionFolder           ← AppWay-controlled working folder
D:\AISolutionFolderArchive    ← Our publisher moves processed final-* here
```

### `D:\AISolutionFolder` (AppWay-controlled)
- `D:\AISolutionFolder\failed` — AppWay's own "failed" folder. AppWay moves folders here if internal processing timeout (`MaxAISolutionFolderProcessTimeInMin = 10`) is exceeded. **Separate from our `s3://appway-bridge-prod/failed/` operator artifact prefix — do not confuse them.**
- Transient folders created by the flow:
  - `receiving-<job-id>` (created by AppWay Link while decrypting)
  - `final-<job-id>` (picked up by our `publisher.py`)
  - `tmp-result-<job-id>` → renamed to `result-<job-id>` (created by our `result_consumer.py`, picked up by AppWay Link for encryption & return)

### AppWay Link (Heidelberg) installation
- Binary tree: `C:\HDAppWayLink\` (DLLs, `MCAISolution.exe`, `MCAISolutionCreatePartner.exe`, etc.)
- Installer cache: `C:\Installers\AppWay\HDAppWayLink`
- Registry config: `HKLM:\SOFTWARE\Wow6432Node\MedicalCommunications\AISolution`
  - `AISolutionFolder = D:\AISolutionFolder`
  - `MaxAISolutionFolderProcessTimeInMin = 10`
  - `MinAgeBeforeHandleResultFolderInSeconds = 1`
  - `TimeBetweenRetriesInSec = 30`
  - `ServiceAISolutionAutomaticCheckSleepTimeInMinutes = 20`
  - `ServiceAISolutionProgramm = MCAISolution.exe`
- Running Windows services (all Automatic + Running):
  - `MedicalCommunications AI Solution Service`
  - `MedicalCommunications Data Exchange Collector`
  - `MedicalCommunications DICOM Distributor`

### Bridge relays (our code)
- Project root: `C:\AppWayBridge\`
- Python venv: `C:\Tools\appway-bridge-venv\` (Python 3.12 at `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\`)
- Tree:
  ```
  C:\AppWayBridge\
    appway_bridge\
      __init__.py
      common.py          (7.5 KB — RelayConfig, state, S3/SQS helpers)
      publisher.py       (3.7 KB — watches final-*, uploads to S3, sends SQS)
      result_consumer.py (4.1 KB — polls SQS, downloads results, drops into result-*)
    bin\
      run-publisher.ps1
      run-result-consumer.ps1
      cleanup-archive.ps1
    logs\
      publisher.log
      result_consumer.log
    state\state.json
    config.json
    requirements.txt
  ```
- `config.json` (verified contents):
  ```json
  {
    "region": "eu-west-1",
    "bucket": "appway-bridge-prod",
    "jobs_queue_url": "https://sqs.eu-west-1.amazonaws.com/911167932273/appway-jobs",
    "results_queue_url": "https://sqs.eu-west-1.amazonaws.com/911167932273/appway-results",
    "ai_solution_folder": "D:\\AISolutionFolder",
    "archive_folder": "D:\\AISolutionFolderArchive",
    "state_file": "C:\\AppWayBridge\\state\\state.json",
    "log_dir": "C:\\AppWayBridge\\logs",
    "poll_seconds": 10,
    "folder_stable_seconds": 20
  }
  ```

### Scheduled Tasks (the relay supervisor model)

| Task | State | Trigger | Runs as | Command |
|------|-------|---------|---------|---------|
| `AppWayBridgePublisher` | Running | At user logon / boot, continuous | SYSTEM | `powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\AppWayBridge\bin\run-publisher.ps1` |
| `AppWayBridgeResultConsumer` | Running | At user logon / boot, continuous | SYSTEM | `powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\AppWayBridge\bin\run-result-consumer.ps1` |
| `AppWayArchiveCleanup` | Ready | Daily 03:00 | SYSTEM (hidden, highest) | `powershell -File C:\AppWayBridge\bin\cleanup-archive.ps1` (deletes `D:\AISolutionFolderArchive\*` older than 7 days) |

Both long-running tasks have `ExecutionTimeLimit=PT72H`; they are restarted by Windows if they die (via Task Scheduler's own restart behavior on failure).

### Known-good smoke results (from `state\state.json`)

Successfully round-tripped jobs:
- `final-test-job` (2026-04-19)
- `final-dicom-examples-20260419-191800` (2026-04-19)
- `final-dicom-examples-20260419-221146` (2026-04-19)
- `backend-test-001`, `epdf-test-001` (result-consumer side)

### Log sanity at last inspection (2026-04-25 08:41)
- `publisher.log` — last line `Publisher started for D:\AISolutionFolder` at `06:54:00`, then idle (no new `final-*` folder to publish). ✅ OK
- `result_consumer.log` — continuously logs `No result messages available` every ~20 s. ✅ OK (healthy polling)

## Useful one-liners

Tail publisher/consumer logs:
```bash
./scripts/ssm_run.py 'Get-Content C:\AppWayBridge\logs\publisher.log      -Tail 40'
./scripts/ssm_run.py 'Get-Content C:\AppWayBridge\logs\result_consumer.log -Tail 40'
```

List live `final-*` and `result-*` folders:
```bash
./scripts/ssm_run.py 'Get-ChildItem D:\AISolutionFolder | Format-Table Name,LastWriteTime'
```

Restart a scheduled task (e.g. after pushing new code):
```bash
./scripts/ssm_run.py 'Stop-ScheduledTask -TaskName AppWayBridgePublisher; Start-Sleep 2; Start-ScheduledTask -TaskName AppWayBridgePublisher'
```
