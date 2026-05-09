# HEYEX 2 Testing Windows EC2

This document captures the state of the dedicated Windows EC2 instance that hosts a
test install of Heidelberg **HEYEX 2** (Spectralis PACS / workstation software). It is
used to exercise the **customer side** of the AppWay pipeline end-to-end from our own
AWS account — i.e. push a DICOM from HEYEX 2 → watch AppWay pick it up → verify the
`result.dcm` returned by `appway-worker` lands back in HEYEX 2.

See also:
- `docs/appway.md` — the production **Solution-side** Windows EC2 that runs AppWay Link.
- `docs/backend.md` — the Linux backend worker that receives jobs via SQS.
- `docs/heyex2/` — Heidelberg's official HEYEX 2 manuals.

---

## Purpose

The original end-to-end validation needed a HEYEX 2 deployment at a real clinic to
actually trigger jobs. To shorten the feedback loop and avoid bothering real ophthalmologists
during integration testing, we now have our own HEYEX 2 instance inside AWS:

- Lives in the same region / VPC as the rest of the AppWay infrastructure.
- Can be stopped between test sessions to save on cost.
- Accessible via RDP (for the HEYEX 2 GUI) and via AWS SSM (for headless automation / log
  gathering from this repo).

It is **not** a production machine and should **never** hold real patient data. Only
fully pseudonymized / synthetic DICOMs allowed.

---

## AWS Instance Details

| Property              | Value |
|-----------------------|-------|
| **Name tag**          | `Heyex2-testing` |
| **Instance ID**       | `i-02a7dd1797d85a099` |
| **Region / AZ**       | `eu-west-1` / `eu-west-1c` |
| **Type**              | `m5.2xlarge` (8 vCPU, 32 GB RAM) |
| **Public IP**         | `54.154.242.69` *(dynamic — may change on stop/start; use SSM Session Manager to avoid relying on it)* |
| **Private IP**        | `172.31.33.103` |
| **VPC**               | `vpc-0dd84caab7d0fb7a3` *(same VPC as backend + AppWay Link)* |
| **Subnet**            | `subnet-02a928111c2b84624` |
| **Security Group**    | `sg-07f6602efab70c442` (`launch-wizard-3`) |
| **Key pair**          | `AppWay` |
| **IAM instance profile** | `EC2Heyex2TestingRole` (with managed policy `AmazonSSMManagedInstanceCore` attached) |

Verified via `aws ec2 describe-instances --instance-ids i-02a7dd1797d85a099` on
2026-04-30.

---

## Hardware / OS Snapshot

Gathered via SSM on 2026-04-30 13:25 UTC (`Get-ComputerInfo` + friends):

| Item          | Value |
|---------------|-------|
| Computer name | `EC2AMAZ-UIM0T5T` (WORKGROUP) |
| OS            | Microsoft **Windows Server 2019 Datacenter** |
| Build         | 10.0.17763 (1809) |
| Architecture  | 64-bit |
| CPU           | Intel Xeon Platinum 8259CL @ 2.50 GHz |
| RAM           | 32 GiB (33,866,407,936 bytes) |
| Disk          | 128 GiB NVMe EBS (Disk 0 — 23.8 GiB used / 104.2 GiB free at first snapshot) |
| SSM Agent     | v3.3.4121.0 — **Online** |

### Noteworthy running services (first snapshot)

Nothing HEYEX-related is installed yet — it's a vanilla Windows Server 2019 baseline
with the usual Microsoft services running. Key services visible:

- `AmazonSSMAgent` (Amazon SSM Agent)
- `TermService` + `UmRdpService` + `SessionEnv` (Remote Desktop)
- `WinRM` (remote PowerShell)
- `Schedule` (Task Scheduler — useful if we want cron-like behaviour later)
- `WinDefend` + `WdNisSvc` + `mpssvc` (Windows Defender + Firewall)
- `Winmgmt` (WMI)

HEYEX 2, the AppWay Link, the publisher/result-consumer relays, Python, .NET ≥ 4.8,
SQL Server etc. are **not** installed yet.

---

## Remote-Access Setup

### 1. IAM / SSM — for headless automation from this repo

On 2026-04-30 the instance was launched **without** an IAM instance profile, which
prevented SSM from managing it. Fixed by creating and attaching a dedicated role:

- **Role name:** `EC2Heyex2TestingRole`
- **Attached managed policy:** `AmazonSSMManagedInstanceCore` (AWS-managed)
- **Attached via:** EC2 Console → Instances → Heyex2-testing → Actions → Security → Modify IAM role

After attaching the role, the SSM Agent did not auto-detect the new credentials on a
running Windows instance. It had to be restarted manually. Inside the machine (via
RDP, Command Prompt):

```cmd
net stop AmazonSSMAgent && net start AmazonSSMAgent
```

Once restarted, the instance registered with SSM within ~30 seconds. Verified from
the backend EC2:

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=i-02a7dd1797d85a099"
# → PingStatus: Online
```

> **Lesson learned.** When launching a future Windows test instance, attach the IAM
> profile **at launch time** so SSM works immediately without a restart dance.
> If you launch without one and attach it later, you have to manually
> `net stop / net start AmazonSSMAgent` to pick up the new credentials.

### 2. RDP — for the HEYEX 2 GUI

Standard Windows RDP on port 3389. Use the `AppWay` key pair to decrypt the
Administrator password from the EC2 Console:

```
EC2 Console → Instances → Heyex2-testing → Connect → RDP client
→ Get password → paste AppWay.pem → reveal password
```

Then from any RDP client:

- Host: `54.154.242.69` (or the current Elastic IP if one is later associated)
- User: `Administrator`
- Password: (decrypted from the console)

### 3. SSM Session Manager — interactive shell without RDP

For quick shell access without opening port 3389:

```bash
aws ssm start-session --target i-02a7dd1797d85a099
```

Opens a PowerShell session on the Windows machine, tunneled through AWS — no RDP
port exposure needed.

---

## Running Commands from the Backend EC2

The backend EC2 role (`EC2AppWayBackendRole`) already carries
`ssm:SendCommand` / `ssm:GetCommandInvocation` on the production AppWay Windows
instance. The Heyex2-testing instance is covered by the same permissions because
they live in the same AWS account — nothing extra was needed in the backend role's
inline policy.

Shortcut helper already in the repo: `scripts/ssm_run.py` — pass the target
instance ID and a PowerShell snippet. Example:

```bash
cd /home/ubuntu/appway-backend
uv run python scripts/ssm_run.py \
  --instance-id i-02a7dd1797d85a099 \
  --ps "Get-Service AmazonSSMAgent | Format-List *"
```

Or a raw one-liner with the AWS CLI:

```bash
CID=$(aws ssm send-command \
  --instance-ids i-02a7dd1797d85a099 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["hostname; whoami"]' \
  --query 'Command.CommandId' --output text)

sleep 3
aws ssm get-command-invocation --command-id "$CID" \
  --instance-id i-02a7dd1797d85a099 --query 'StandardOutputContent' --output text
```

---

## IAM Policies Required on the Backend EC2 Role

Nothing new was needed to talk to this machine. The existing backend role already
allows:

- `ssm:SendCommand` / `ssm:GetCommandInvocation` / `ssm:ListCommandInvocations`
  on `arn:aws:ec2:eu-west-1:*:instance/*` (wildcard across all instances in the account).
- `ec2:DescribeInstances` / `ec2:DescribeInstanceStatus`.

Confirmed end-to-end with command id `9d15309d-8505-4b3c-be9a-af1023823d0f`
(status `Success`, `ResponseCode: 0`).

**What the backend role is still NOT allowed to do** (by design — least privilege):

- `iam:ListAttachedRolePolicies`, `iam:Get*` — cannot inspect IAM roles at all.
- `ec2:DescribeSecurityGroups`, `ec2:DescribeVpcEndpoints` — cannot see network config.

These omissions do not block any Heyex2-testing workflow we care about; they just
mean security-group / VPC troubleshooting has to happen in the AWS Console or from a
machine with wider IAM access.

---

## Current State (2026-05-01)

- ✅ Instance running, reachable on 54.154.242.69.
- ✅ IAM instance profile attached → SSM Agent online → headless automation works.
- ✅ **AWS CLI v2.34.40** installed at `C:\Program Files\Amazon\AWSCLIV2\aws.exe`
  (installed via SSM on 2026-05-01, command `c7a07606-8447-4021-8e4b-907cc189a298`).
- ✅ **IAM inline policy `S3ReadAppwayPackage`** attached to `EC2Heyex2TestingRole`
  — grants `s3:GetObject` on `arn:aws:s3:::appway-package/heyex2/*` and
  `s3:ListBucket` (scoped to `heyex2/` prefix).
- ✅ **HEYEX PACS installer zip downloaded and extracted**:
  `C:\Installers\Heyex2\HEYEX_PACS_2.6.10\HEYEX PACS 2.6.10 Build 2248 I4.0\`
  (4.05 GB, extracted by user via RDP).
- ✅ **Pre-install machine prep completed** (2026-05-01 ~15:30–15:37 UTC, via SSM):
  - .NET Framework 4.8 confirmed (release key 528049).
  - `C:\` free space: 95.8 GB / 128 GB.
  - Page file set to **fixed 32 GB / 32 GB** on `C:\pagefile.sys`
    (`AutomaticManagedPagefile = False`).
  - Windows Defender exclusions: **5 folder paths** (`C:\HEYEX`,
    `C:\Program Files\HEYEX`, `C:\Program Files (x86)\SQL Anywhere 17\Bin64`,
    `C:\Users\Administrator\AppData\Local\Temp`, `C:\Installers\Heyex2`) +
    **12 file extensions** (`.dcm .bmp .tcl .inf .bin .db .log .e2e .edb .pdb .sdb .mdb`).
  - Windows Firewall inbound rules (TCP, Any profile):
    `HEYEX2-DICOM` (104,105), `HEYEX2-Database` (2638,40001),
    `HEYEX2-DICOM-TLS` (2762), `HEYEX2-CIFS` (445),
    `HEYEX2-WEB` (443), `HEYEX2-HL7` (5678–5681) — all **Enabled**.
  - HEYEX target directories created:
    `C:\HEYEX\{Database, ImagePool, MainImport, TransactionLogs, UVOBackup}`.
  - All installer `.exe`/`.msi` files unblocked (10 files via `Unblock-File`).
  - Machine rebooted; SSM came back Online within ~90 s.
- ✅ **HEYEX 2 v2.6.10 (Build 2248) installed** — see Verified entry 2026-05-01 ~16:14 UTC.
  - Modules installed: HEYEX 2 base, HEYEX 2 Update 2.6.10, Spectralis Viewing Module
    7.0.11.0, Secondary Data Factory Module (SEDAF) 1.0.17.0.
  - Acquisition Module (AQM) **not installed** (no physical SPECTRALIS device on EC2).
  - Install root: `C:\HEYEX` / binaries: `C:\Program Files\HEYEX`.
  - Database: `C:\HEYEX\Database` (Sybase SQL Anywhere 17 — `MCAshvins` + `M3iArchive`).
  - ImagePool: `C:\HEYEX\ImagwPool` (note: installer used its own spelling — typo in
    HEYEX default; a separate `C:\HEYEX\ImagePool` dir also exists from pre-prep).
  - MainImport hot-folders: `C:\HEYEX\MainImport\Import1..4`, `ImportUVO`, `ImportHL7`,
    `ImportCD`, `ImportMarketplace`, `ImportE2E1..2`.
  - Licence: **demo / grace mode** (`LicenseCheckingGraceTimeIntervalInHours=1`,
    `LicenseCriticalGracetimeInHours=10`).
  - **DICOM AE titles:** `GlobalCallingAET=HEYEX2TEST`, `GlobalCalledAET=Me`,
    store port 104, query port 105.
    `MedicalCommunications DICOM Server` restarted to pick up the change.

- ✅ **Dongle bridge active** — Marx Crypto Box CBU forwarded via Tailscale + VirtualHere.
  See `docs/heyex2-dongle.md` for full SOP and architecture.
  - Local PC Tailscale IP: `100.64.25.24`, VirtualHere Server on TCP 7575
  - EC2 VH Client: `C:\VirtualHere\vhui64.exe` (launch in RDP session, config: `vhui.ini`)
  - Dongle bound as `CBUSB Ver 2.0` (VID_0D7A&PID_0001), Status OK
  - `HELICSVC` restarted → License Manager → "Marx Crypto Box CBU" activated
  - HEYEX 2 v2.6.10 running, logged in as `sysadmin`/`hesmc` ✓

- ⬜ AppWay Link client / credentials — not installed yet (separate step;
  see `docs/next-steps.md` → V2).

See the **Next Steps** section below for what has to happen before this machine can
actually push a test DICOM into AppWay.

---

## Next Steps (to make this machine useful)

1. **Install HEYEX 2 prerequisites**
   - Microsoft SQL Server (edition per Heidelberg's manual — `docs/heyex2/Ashvins_HEYEX_2.6.2_EN_IT_and_Hardware_Requirements.pdf`).
   - .NET Framework ≥ 4.8 (should already be present on Windows Server 2019).
   - IIS if required.
2. **Install HEYEX 2 itself**
   - Use the Heidelberg installer provided by MedicalCommunications.
   - User manual: `docs/heyex2/Ashvins_HEYEX_2.6.2_EN_User_Manual.pdf`.
3. **Install the AppWay Link client**
   - Same binary used on the production AppWay Windows EC2 (`docs/appway.md`).
   - Needs the customer private key issued by Heidelberg for the test environment.
4. **Install our publisher / result-consumer relays** (if this machine is meant to
   also act as the customer-side bridge — *probably not*: its job is to simulate
   HEYEX, not AppWay Link).
5. **Configure DICOM AE titles / ports** so HEYEX 2 can push to AppWay Link and
   receive the result object back from it.
6. **Smoke test** — push a test OPT volume from HEYEX 2, watch it land in
   `s3://appway-bridge-prod/incoming/`, and verify HEYEX 2 eventually receives the
   `result.dcm` back.

Each of these steps will be added to this document (with command snippets,
screenshots if needed, and dated verification entries) as they are completed — same
format as `docs/appway.md`.

---

## Verified

- **2026-04-30 13:24 UTC** — IAM role `EC2Heyex2TestingRole` attached to instance
  `i-02a7dd1797d85a099`; SSM Agent restarted inside the machine via
  `net stop / net start AmazonSSMAgent`; instance showed up in SSM inventory
  within ~30 s (`AgentVersion: 3.3.4121.0, PingStatus: Online`). End-to-end
  PowerShell command run from the backend EC2 succeeded (command id
  `9d15309d-8505-4b3c-be9a-af1023823d0f`, `Status: Success`). ✓

- **2026-05-01 09:19 UTC** — AWS CLI v2 installed on the Windows EC2 via SSM
  (`msiexec /qn`, command `c7a07606-8447-4021-8e4b-907cc189a298`); confirmed
  `aws-cli/2.34.40 Python/3.14.4 Windows/2019Server exec-env/EC2 exe/AMD64`. ✓

- **2026-05-01 09:23 UTC** — Inline policy `S3ReadAppwayPackage` added to
  `EC2Heyex2TestingRole` by hand in IAM Console; grants `s3:GetObject` on
  `arn:aws:s3:::appway-package/heyex2/*` and `s3:ListBucket` scoped to
  `heyex2/` prefix. ✓

- **2026-05-01 09:26–09:28 UTC** — HEYEX PACS 2.6.10 installer zip downloaded
  from `s3://appway-package/heyex2/HEYEX PACS 2.6.10 Build 2248 I4.0.zip` to
  `C:\Installers\Heyex2\HEYEX_PACS_2.6.10.zip` via `aws s3 cp` run through SSM
  (command `8ca538ae-8993-4c31-9014-22b590f17541`). Final file size on disk:
  **4,351,789,073 bytes (4.05 GB)** — matches S3 source. ✓

- **2026-05-01 15:30–15:37 UTC** — Pre-install machine prep completed via SSM
  (commands `a4909b7e`, `12769c2f`, `e3e3b347`, `b20e98bb`):
  - .NET Framework **4.8** confirmed (registry release key `528049`).
  - `C:\` free: **95.8 GB** / 128 GB total.
  - Page file: **fixed 32768 MB / 32768 MB** on `C:\pagefile.sys`;
    `AutomaticManagedPagefile = False`.
  - Windows Defender: **5 folder exclusions** + **12 extension exclusions** added.
  - Windows Firewall: **6 inbound TCP rules** created and enabled
    (`HEYEX2-DICOM` 104/105, `HEYEX2-Database` 2638/40001,
    `HEYEX2-DICOM-TLS` 2762, `HEYEX2-CIFS` 445, `HEYEX2-WEB` 443,
    `HEYEX2-HL7` 5678–5681).
  - HEYEX target directories pre-created:
    `C:\HEYEX\{Database, ImagePool, MainImport, TransactionLogs, UVOBackup}`.
  - All 10 installer `.exe`/`.msi` files unblocked via `Unblock-File`.
  - Machine rebooted; SSM came back `Online` in ~90 s. ✓

- **2026-05-01 15:48–16:13 UTC** — **HEYEX 2 v2.6.10 (Build 2248) installed** via
  `Setup.exe` (Heidelberg Engineering Master Installer) run as Administrator over RDP.
  Modules selected: HEYEX 2 2248, SPECTRALIS Secondary Data Factory Module 1.0.17.0,
  SPECTRALIS Viewing Module 7.0.11.0. Acquisition Module left unchecked (no physical
  device). Paths chosen: install root `C:\HEYEX`, DB `C:\HEYEX\Database`,
  ImagePool `C:\HEYEX\ImagwPool`, MainImport `C:\HEYEX\MainImport`.
  Machine rebooted at end of install. ✓

- **2026-05-01 16:14–16:16 UTC** — Post-install verification via SSM
  (commands `d411e562`, `4c0e4f3a`). All checks passed:
  - **36 MedicalCommunications services** registered; **22 Running** (core set),
    remainder Stopped (optional/on-demand services — expected).
  - Key Running services: `DICOM Server`, `DICOM Import`, `DICOM Import Post Process`,
    `DICOM Archive`, `DICOM Distributor`, `DICOM Restore`, `DICOM Storage Commitment`,
    `AMPI Core/HL7/Send`, `Task`, `UVO`, `XIS Core`, `Database Backup`,
    `Data Exchange Collector`, `Server Data Handling Modules`, `User Notification`,
    `ACQ Export Service`.
  - **2× Sybase SQL Anywhere 17** DB engines running:
    `SQL Anywhere - EOL_HEYEX` + `SQL Anywhere - ACQ_EC2AMAZ-UIM0T5T`.
  - **Sentinel RMS License Manager** running (demo/grace mode).
  - **Listening ports confirmed**: TCP 104, 105 (DICOM), 443 (WEB), 2638 (DB),
    40001 (DB internal). DICOM-TLS 2762 not yet listening (expected — SSL not
    configured).
  - **Registry**: `AshvinsProfessionalVersion=2.6.10`, `AshvinsProfessionalBuild=2248`,
    `MIII_HOME=C:\HEYEX`, `GlobalPort=104`, `GlobalQueryPort=105`,
    `GlobalCallingAET=You` *(⚠️ to be changed)*, `GlobalCalledAET=Me`.
  - **4 Windows user accounts** created by installer and Enabled:
    `ashvinsloc`, `heyexuser`, `Imagepooluser`, `mcsystem`.
  - **Free disk**: **65.5 GB** remaining on `C:\` (30.3 GB consumed by install).
  - **Event log**: zero HEYEX/MedicalCommunications/SQLAny errors in Application log. ✓

- **2026-05-01 16:19 UTC** — DICOM Calling AE title changed from default `You` →
  **`HEYEX2TEST`** via registry write to
  `HKLM\SOFTWARE\WOW6432Node\MedicalCommunications\Options\GlobalCallingAET`
  (SSM command `68108fa4`). `MedicalCommunications DICOM Server` restarted and
  confirmed **Running**. HEYEX 2 will now identify itself as `HEYEX2TEST` on
  all outgoing DICOM associations. ✓

- **2026-05-09 21:38 UTC** — **Marx Crypto Box CBU dongle bridge operational.**
  Full Tailscale + VirtualHere USB-over-IP bridge established between local Windows 11
  PC and the Heyex2-testing EC2. HEYEX 2 v2.6.10 licensed and running. Details:
  - Tailscale mesh: local PC `100.64.25.24` (MSI) ↔ EC2 `100.79.248.90`. TCP 7575
    confirmed reachable (SSM command `dec6c45c`).
  - Marx CBU driver (`CBUSetup_13Oct2025.zip`, from marx.com/en/support/downloads)
    installed on local PC. Dongle LED: red (ready).
  - VirtualHere USB Server running as Windows service on local PC; TCP 7575 open
    in Windows Firewall.
  - VirtualHere Client v5.9.8 (`vhui64.exe`) launched in Administrator RDP session
    (Session 2); config `C:\Users\Administrator\AppData\Roaming\vhui.ini` with
    `SERVER=100.64.25.24:7575` and `AUTOUSE USB CrypToken=1`.
  - Dongle forwarded: VH client tree showed **"USB CrypToken (In use by you)"**.
  - EC2 Device Manager: `CBUSB Ver 2.0` (VID_0D7A&PID_0001) Status **OK** — bound
    to Marx driver `cbu2_64.inf` / `cbusb_64.inf` (pre-installed by HEYEX 2 setup).
  - `HELICSVC` (Heidelberg Eye Explorer License Manager) restarted via SSM
    (command `0a43b0f8`).
  - License Manager tray → clicked **"Marx Crypto Box CBU"** image → license activated.
  - HEYEX 2 launched; logged in as `sysadmin` / `hesmc` — **fully operational**. ✓
  - See `docs/heyex2-dongle.md` for full SOP, architecture diagram, and troubleshooting.
