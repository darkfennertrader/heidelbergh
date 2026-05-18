<#
.SYNOPSIS
  Session keep-alive for the AppWay Link Windows EC2.
  Prevents Windows from throttling the MCAshvinsWorkstation polling
  thread due to an idle / disconnected RDP desktop session.

.DESCRIPTION
  Sends a harmless F15 keystroke every 60 seconds via WScript.Shell.
  F15 has no effect on any application but resets Windows' LastInputInfo
  timestamp, keeping the desktop session "active" from Windows' point of view.

  This prevents the WebView2 compositor (and the AIS polling thread that
  depends on it) from being throttled to near-zero scheduling priority when
  no human is moving the mouse.

  Deployed as scheduled task 'AppWayKeepAlive':
    - Trigger: At logon of the interactive user (Administrator)
    - Action: powershell.exe -WindowStyle Hidden -NonInteractive
                             -File C:\AppWayBridge\bin\keep_session_active.ps1
    - Run only when user is logged on
    - Do not stop task if computer switches to battery power

.NOTES
  This is a WORKAROUND for a Heidelberg defect: MCAshvinsWorkstation's
  D:\AISolutionFolder polling thread should not depend on an active
  desktop session. The structural fix must come from Heidelberg.
  Reference: docs/test_results/round12-summary.md, round12-verbose-log-evidence.txt
  Ticket: Rouwen Heidelberg — polling stall investigation (2026-05-18)

.DEPLOYMENT
  From the Linux backend EC2, run:
    python3 scripts/ssm_run.py --instance heyex2 -c "
      \$src = 'C:\AppWayBridge\bin\keep_session_active.ps1'
      # (copy script content here or upload via S3)
      Register-ScheduledTask \
        -TaskName 'AppWayKeepAlive' \
        -Trigger (New-ScheduledTaskTrigger -AtLogon) \
        -Action (New-ScheduledTaskAction -Execute 'powershell.exe' \
                   -Argument '-WindowStyle Hidden -NonInteractive -File C:\AppWayBridge\bin\keep_session_active.ps1') \
        -RunLevel Limited \
        -Force
    "
#>

$ErrorActionPreference = 'Continue'
$LogFile = 'C:\AppWayBridge\logs\keepalive.log'
$IntervalSeconds = 60

# Ensure log directory exists
$logDir = Split-Path $LogFile
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Write-KALog {
    param([string]$Line)
    $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    "$stamp  $Line" | Out-File -FilePath $LogFile -Append -Encoding UTF8
}

Write-KALog "AppWayKeepAlive started (interval=${IntervalSeconds}s, PID=$PID)"

$wsh = New-Object -ComObject WScript.Shell

$tick = 0
while ($true) {
    # Send F15 — a key with no application binding
    # This resets Windows' LastInputInfo without affecting any open window
    $wsh.SendKeys('{F15}')

    $tick++
    # Log once every 10 ticks (every ~10 min) to confirm liveness without flooding
    if ($tick % 10 -eq 0) {
        Write-KALog "keepalive tick $tick  (${IntervalSeconds}s * $tick = $($tick * $IntervalSeconds)s elapsed)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
