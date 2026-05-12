<#
.SYNOPSIS
  AppWay relay health check. Alerts via SNS when a relay scheduled task
  is not in the Running state.

.DESCRIPTION
  Runs every 5 minutes as SYSTEM on the AppWay Windows EC2. Checks the
  state of the two relay scheduled tasks:
    - AppWayBridgePublisher
    - AppWayBridgeResultConsumer

  Both are supposed to be permanently Running (they wrap an infinite
  loop). If either is not Running, publish a one-line alert to the
  operator SNS topic.

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
  'AppWayBridgeResultConsumer'
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

if ($failed.Count -eq 0) {
  Write-HealthLog "OK  all relay tasks Running"
  exit 0
}

# --- Build alert ---
$timestamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$lines = @(
  "AppWay relay health check failed on ${Hostname} (${Instance}) at ${timestamp} UTC.",
  "",
  "The following relay scheduled task(s) are NOT in state 'Running':",
  ""
)
foreach ($f in $failed) {
  $lines += "  - $($f.Task): state=$($f.State) lastRun=$($f.LastRunTime) lastResult=$($f.LastTaskResult)"
}
$lines += @(
  "",
  "Impact: AppWay jobs may stop being relayed between HEYEX and the backend worker.",
  "",
  "Next steps:",
  "  1. RDP / SSM into $Instance and run:",
  "       Get-ScheduledTask | Where-Object TaskName -like 'AppWay*' | Select TaskName,State",
  "  2. Inspect the relay log under D:\AppWayBridge\logs (publisher.log / result_consumer.log).",
  "  3. Start-ScheduledTask -TaskName <TaskName> to restart a failed relay.",
  "",
  "This alert was sent by the AppWayHealthCheck scheduled task (see docs/appway.md)."
)
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
    Write-HealthLog "ALERT sent  failedTasks=$failedNames  sns=OK"
  } else {
    Write-HealthLog "ALERT publish FAILED  rc=$rc  output=$pubOut"
  }
} catch {
  Write-HealthLog "ALERT publish EXCEPTION  $($_.Exception.Message)"
}

exit 0
