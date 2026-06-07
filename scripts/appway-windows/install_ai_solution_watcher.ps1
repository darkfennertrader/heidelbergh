# install_ai_solution_watcher.ps1
# ---------------------------------------------------------------------------
# Installs a polling-based watcher on the AppWay Link instance.
#
# What it does:
#   Every 2 seconds, checks D:\AISolutionFolder for new "result-*" folders.
#   When one appears, waits 3 s for result.dcm to finish writing, then
#   restarts "MedicalCommunications AI Solution Service" so the service
#   picks up the result on its very first poll cycle (~5-10 s after restart)
#   instead of waiting for the next natural ServiceAISolutionAutomaticCheckSleepTimeInMinutes
#   cycle (≤ 60 s when registry is REG_SZ "1", or up to 20 min if it ever reverts to REG_DWORD).
#
# Registry type note (root cause identified 2026-05-26):
#   MCAISolutionService.exe v1.2.2031.0 overwrites the polling-interval value
#   to 20 on startup ONLY when the registry value type is REG_DWORD. If the type
#   is REG_SZ (string), the service leaves the value untouched. The value on this
#   instance is therefore kept as REG_SZ "1". This watcher is retained as a
#   belt-and-braces safety net in case a future AppWay upgrade re-creates the value
#   as REG_DWORD (which would silently restore 20-min latency without any other alarm).
#   The healthcheck.ps1 also alerts if the value type ever reverts to REG_DWORD.
#
# Usage (run once via SSM or locally on the AppWay Link):
#   powershell -ExecutionPolicy Bypass -File install_ai_solution_watcher.ps1
#
# Idempotent: safe to re-run; will overwrite previous installation.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

$installDir  = 'C:\AppWayBridge'
$watcherPath = "$installDir\ai_solution_watcher.ps1"
$logPath     = "$installDir\watcher.log"
$taskName    = 'AppWay-AISolutionFolder-Watcher'
$watchFolder = 'D:\AISolutionFolder'
$serviceName = 'MedicalCommunications AI Solution Service'

# ── 1. Create install directory ──────────────────────────────────────────────
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir | Out-Null
}
Write-Host "[install] Install dir: $installDir"

# ── 2. Write the watcher runner script ───────────────────────────────────────
# Uses a simple polling loop — all vars in same runspace, no scope issues.
$watcherScript = @'
# ai_solution_watcher.ps1  — managed by install_ai_solution_watcher.ps1
$logPath     = 'C:\AppWayBridge\watcher.log'
$watchFolder = 'D:\AISolutionFolder'
$serviceName = 'MedicalCommunications AI Solution Service'
$pollSecs    = 2
$settlesSecs = 3

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "$ts  $msg"
    Add-Content -Path $logPath -Value $line
    Write-Host $line
}

Log "Watcher started. Monitoring: $watchFolder (poll every ${pollSecs}s)"

# Snapshot folders that already exist so we don't trigger on them
$seen = @{}
Get-ChildItem $watchFolder -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $seen[$_.Name] = $true
}
Log "Pre-existing folders snapshotted: $($seen.Count)"

while ($true) {
    Start-Sleep -Seconds $pollSecs

    try {
        $dirs = Get-ChildItem $watchFolder -Directory -ErrorAction SilentlyContinue
    } catch {
        Log "WARNING: could not list $watchFolder — $_"
        continue
    }

    foreach ($d in $dirs) {
        if ($d.Name -like 'result-*' -and -not $seen.ContainsKey($d.Name)) {
            $seen[$d.Name] = $true
            Log "New result folder detected: $($d.Name)"
            Log "Waiting ${settlesSecs}s for result.dcm to finish writing..."
            Start-Sleep -Seconds $settlesSecs

            Log "Restarting '$serviceName'..."
            try {
                Restart-Service -Name $serviceName -Force -ErrorAction Stop
                Log "Service restarted OK. Result should be delivered within ~30s."
            } catch {
                Log "ERROR restarting service: $_"
            }
        }
    }
}
'@

Set-Content -Path $watcherPath -Value $watcherScript -Encoding UTF8
Write-Host "[install] Wrote watcher script: $watcherPath"

# ── 3. Remove existing scheduled task if present ─────────────────────────────
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask  -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "[install] Removed existing task '$taskName'"
}

# ── 4. Register new scheduled task ───────────────────────────────────────────
$psArgs  = "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$watcherPath`""
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId    'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel  Highest

Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "[install] Registered scheduled task '$taskName'"

# ── 5. Start the task now (no reboot needed) ──────────────────────────────────
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 4

$state = (Get-ScheduledTask -TaskName $taskName).State
Write-Host "[install] Task state: $state"

# ── 6. Show first log lines ───────────────────────────────────────────────────
Start-Sleep -Seconds 3
if (Test-Path $logPath) {
    Write-Host "[install] Watcher log (last 5 lines):"
    Get-Content $logPath | Select-Object -Last 5
} else {
    Write-Host "[install] WARNING: log not yet created"
}

Write-Host ""
Write-Host "=== Installation complete ==="
Write-Host "  Script  : $watcherPath"
Write-Host "  Log     : $logPath"
Write-Host "  Task    : $taskName  (state: $state)"
Write-Host "  Watch   : $watchFolder  (result-* folders, poll every 2s)"
Write-Host "  Action  : Restart-Service '$serviceName'"
