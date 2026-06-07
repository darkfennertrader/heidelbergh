<#
.SYNOPSIS
  AppWay relay health check. Alerts via SNS when a relay scheduled task
  is not in the Running state, or when the AI Solution polling-interval
  registry value has reverted to REG_DWORD (which causes MCAISolutionService
  to overwrite it to 20 on next restart, silently restoring 20-min latency).

.DESCRIPTION
  Runs every 5 minutes as SYSTEM on the AppWay Windows EC2. Checks:
    1. Scheduled task state for:
         - AppWayBridgePublisher
         - AppWayBridgeResultConsumer
         - AppWay-AISolutionFolder-Watcher
       Both relay tasks must be permanently Running (they wrap an infinite loop).
    2. Registry value type for ServiceAISolutionAutomaticCheckSleepTimeInMinutes:
       Must be REG_SZ (not REG_DWORD). If REG_DWORD, MCAISolutionService will
       overwrite it to 20 on next service restart (root cause identified 2026-05-26).

  If any check fails, publish a one-line alert to the operator SNS topic.

  Appends a line to C:\AppWayBridge\logs\healthcheck.log on every run
  so operators can confirm the check itself is alive.

.NOTES
  Deployed as scheduled task 'AppWayHealthCheck' (boot trigger + every
  5 minutes). Requires IAM permission sns:Publish on the target topic
  (granted via inline policy AppWayDlqAlertsPublish on role
  EC2AppWayBridgeRole).
#>

$ErrorActionPreference = 'Continue'

$TopicArn = 'arn:aws:sns:eu-west-1:911167932273:appway-dlq-alerts'
$Region   = 'eu-west-1'
$LogDir   = 'C:\AppWayBridge\logs'
$LogFile  = Join-Path $LogDir 'healthcheck.log'
$Instance = 'i-02a99abeba370f0a7'
$Hostname = $env:COMPUTERNAME

# Scheduled tasks that should always be Running
$RequiredTasks = @(
  'AppWayBridgePublisher',
  'AppWayBridgeResultConsumer',
  'AppWay-AISolutionFolder-Watcher'   # AI Solution Service restart watcher (2026-05-18)
)

# Guard: ensure log dir exists
if (-not (Test-Path $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-HealthLog {
  param([string]$Line)
  $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
  "$stamp  $Line" | Out-File -FilePath $LogFile -Append -Encoding UTF8
}

$failed = @()
foreach ($name in $RequiredTasks) {
  try {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    if ($task.State -ne 'Running') {
      $info   = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
      $last   = if ($info) { $info.LastRunTime } else { '(unknown)' }
      $result = if ($info) { $info.LastTaskResult } else { '(unknown)' }
      $failed += [pscustomobject]@{
        Task           = $name
        State          = $task.State
        LastRunTime    = $last
        LastTaskResult = $result
      }
    }
  } catch {
    $failed += [pscustomobject]@{
      Task           = $name
      State          = "ERROR: $($_.Exception.Message)"
      LastRunTime    = '(n/a)'
      LastTaskResult = '(n/a)'
    }
  }
}

# ── Registry type guard ───────────────────────────────────────────────────────
# MCAISolutionService.exe overwrites ServiceAISolutionAutomaticCheckSleepTimeInMinutes
# to 20 on startup ONLY when the value type is REG_DWORD. The value must stay as
# REG_SZ "1" to survive service restarts. Alert if it has reverted to REG_DWORD
# (e.g. after an AppWay upgrade that re-creates the installer default).
$regKey   = 'HKLM:\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution'
$regName  = 'ServiceAISolutionAutomaticCheckSleepTimeInMinutes'
$regAlert = $null
try {
  $regItem = Get-Item -Path $regKey -ErrorAction Stop
  $regKind = $regItem.GetValueKind($regName)   # Microsoft.Win32.RegistryValueKind enum
  if ($regKind -eq [Microsoft.Win32.RegistryValueKind]::DWord) {
    $regVal  = $regItem.GetValue($regName)
    $regAlert = "REGISTRY TYPE ALERT: $regName is REG_DWORD (value=$regVal). " +
                "MCAISolutionService will overwrite it to 20 on next restart, " +
                "restoring 20-min AppWay latency. Fix: delete the value and " +
                "recreate as REG_SZ `"1`" (regedit: New > String Value)."
  }
} catch {
  $regAlert = "REGISTRY CHECK ERROR: could not read $regKey\$regName — $($_.Exception.Message)"
}

if ($failed.Count -eq 0 -and -not $regAlert) {
  Write-HealthLog "OK  all relay tasks Running  registry REG_SZ OK"
  exit 0
}

# --- Build alert ---
$timestamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$lines = @(
  "AppWay health check FAILED on ${Hostname} (${Instance}) at ${timestamp} UTC.",
  ""
)

if ($failed.Count -gt 0) {
  $lines += "The following relay scheduled task(s) are NOT in state 'Running':"
  $lines += ""
  foreach ($f in $failed) {
    $lines += "  - $($f.Task): state=$($f.State) lastRun=$($f.LastRunTime) lastResult=$($f.LastTaskResult)"
  }
  $lines += ""
  $lines += "Impact: AppWay jobs may stop being relayed between HEYEX and the backend worker."
  $lines += ""
  $lines += "Next steps:"
  $lines += "  1. RDP / SSM into $Instance and run:"
  $lines += "       Get-ScheduledTask | Where-Object TaskName -like 'AppWay*' | Select TaskName,State"
  $lines += "  2. Inspect the relay log under C:\AppWayBridge\logs (publisher.log / result_consumer.log)."
  $lines += "  3. Start-ScheduledTask -TaskName <TaskName> to restart a failed relay."
  $lines += ""
}

if ($regAlert) {
  $lines += $regAlert
  $lines += ""
  $lines += "To fix the registry type:"
  $lines += "  Option A (regedit GUI): navigate to"
  $lines += "    HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution"
  $lines += "    Delete ServiceAISolutionAutomaticCheckSleepTimeInMinutes, then"
  $lines += "    New > String Value, name it the same, set value data to 1."
  $lines += "  Option B (PowerShell / SSM):"
  $lines += "    Remove-ItemProperty -Path 'HKLM:\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution' -Name ServiceAISolutionAutomaticCheckSleepTimeInMinutes"
  $lines += "    New-ItemProperty    -Path 'HKLM:\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution' -Name ServiceAISolutionAutomaticCheckSleepTimeInMinutes -Value '1' -PropertyType String"
  $lines += ""
}

$lines += "This alert was sent by the AppWayHealthCheck scheduled task (see docs/appway.md)."
$message = $lines -join "`r`n"
$subject = "[AppWay] Relay health check FAILED on $Hostname"

# Publish to SNS via AWS CLI (already installed at C:\Program Files\Amazon\AWSCLIV2\aws.exe)
try {
  $tmp = New-TemporaryFile
  $message | Out-File -FilePath $tmp -Encoding UTF8 -NoNewline

  $pubOut = & aws sns publish `
    --region $Region `
    --topic-arn $TopicArn `
    --subject $subject `
    --message "file://$($tmp.FullName)" 2>&1
  $rc = $LASTEXITCODE
  Remove-Item $tmp -ErrorAction SilentlyContinue

  if ($rc -eq 0) {
    $failedNames = ($failed | ForEach-Object { $_.Task }) -join ','
    $regPart = if ($regAlert) { '  registryTypeAlert=YES' } else { '' }
    Write-HealthLog "ALERT sent  failedTasks=$failedNames${regPart}  sns=OK"
  } else {
    Write-HealthLog "ALERT publish FAILED  rc=$rc  output=$pubOut"
  }
} catch {
  Write-HealthLog "ALERT publish EXCEPTION  $($_.Exception.Message)"
}

exit 0
