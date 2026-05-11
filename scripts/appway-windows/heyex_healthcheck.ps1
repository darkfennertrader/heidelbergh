# heyex_healthcheck.ps1
# Pre-session health check for HEYEX 2 EC2 (Heyex2-testing, i-02a7dd1797d85a099)
# Run via: cat scripts/appway-windows/heyex_healthcheck.ps1 | python3 scripts/ssm_run.py --instance heyex2 -

$sep = "=" * 50

# ── 1. Disk space ─────────────────────────────────────────────────────────────
Write-Host $sep
$c    = Get-PSDrive C
$used = [math]::Round($c.Used / 1GB, 1)
$free = [math]::Round($c.Free / 1GB, 1)
$status = if ($free -lt 5) { "WARNING: Low disk!" } else { "OK" }
Write-Host "DISK   Used=${used}GB  Free=${free}GB  [$status]"

# ── 2. HELICSVC (Heidelberg license service) ──────────────────────────────────
Write-Host $sep
$svc = Get-Service HELICSVC -ErrorAction SilentlyContinue
if ($svc) {
    $st = if ($svc.Status -eq "Running") { "OK" } else { "WARNING" }
    Write-Host "HELICSVC  Status=$($svc.Status)  StartType=$($svc.StartType)  [$st]"
} else {
    Write-Host "HELICSVC  NOT FOUND  [ERROR]"
}

# ── 3. VirtualHere client process ─────────────────────────────────────────────
Write-Host $sep
if (Get-Process vhui64 -ErrorAction SilentlyContinue) {
    Write-Host "VHUI64   RUNNING  [OK]"
} else {
    Write-Host "VHUI64   NOT running  [WARNING - will start at RDP logon]"
}

# ── 4. vhui.ini content ───────────────────────────────────────────────────────
Write-Host $sep
Write-Host "VHUI.INI:"
$ini = "$env:APPDATA\vhui.ini"
if (Test-Path $ini) {
    Get-Content $ini | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "   NOT FOUND at $ini  [WARNING]"
}

# ── 5. VH server TCP reachability ─────────────────────────────────────────────
Write-Host $sep
$r = Test-NetConnection 100.97.57.68 -Port 7575 -WarningAction SilentlyContinue
$st = if ($r.TcpTestSucceeded) { "OK" } else { "ERROR - Tailscale or VH server down!" }
Write-Host "VH 100.97.57.68:7575  reachable=$($r.TcpTestSucceeded)  [$st]"

# ── 6. BeyondTrust / Heidelberg portal reachability ──────────────────────────
Write-Host $sep
$r2 = Test-NetConnection remote.heidelbergengineering.com -Port 443 -WarningAction SilentlyContinue
$st2 = if ($r2.TcpTestSucceeded) { "OK" } else { "ERROR - cannot reach BT portal!" }
Write-Host "BeyondTrust remote.heidelbergengineering.com:443  reachable=$($r2.TcpTestSucceeded)  [$st2]"

# ── 7. Tailscale status ───────────────────────────────────────────────────────
Write-Host $sep
Write-Host "TAILSCALE:"
$ts = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $ts) {
    & $ts status 2>&1 | Select-Object -First 8 | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "   tailscale.exe not found  [WARNING]"
}

# ── 8. Pending Windows reboot ─────────────────────────────────────────────────
Write-Host $sep
$rb = $false
if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") { $rb = $true }
if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired") { $rb = $true }
$st = if ($rb) { "WARNING - reboot required! Reboot before the session." } else { "OK" }
Write-Host "PENDING REBOOT  $rb  [$st]"

# ── 9. HEYEX 2 binary ─────────────────────────────────────────────────────────
Write-Host $sep
Write-Host "HEYEX2 BINARY:"
# HEYEX 2 installs to C:\HEYEX\Heyex.exe (not Program Files)
$heyexExe = "C:\HEYEX\Heyex.exe"
if (Test-Path $heyexExe) {
    $ver = (Get-ItemProperty $heyexExe).VersionInfo.FileVersion
    Write-Host "   $heyexExe  version=$ver  [OK]"
} else {
    # Fallback recursive search
    $found = @()
    $found += Get-ChildItem "C:\HEYEX" -Recurse -Filter Heyex.exe -ErrorAction SilentlyContinue
    if ($found) { $found | ForEach-Object { Write-Host "   $($_.FullName)  [OK]" } }
    else         { Write-Host "   Heyex.exe NOT FOUND  [ERROR]" }
}

# ── 10. Browser ───────────────────────────────────────────────────────────────
Write-Host $sep
$edge   = Test-Path "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$chrome = Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
$ie     = Test-Path "C:\Program Files (x86)\Internet Explorer\iexplore.exe"
$bst = if ($edge -or $chrome -or $ie) { "OK" } else { "WARNING - no browser found for BT portal" }
Write-Host "BROWSER  Edge=$edge  Chrome=$chrome  IE=$ie  [$bst]"
Write-Host "   Note: BeyondTrust supports IE. Edge/Chrome preferred but not required."

Write-Host $sep
Write-Host "Health check complete."
