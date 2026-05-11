# heyex_prep_bt_session.ps1
# Prepares the HEYEX 2 EC2 for a Heidelberg BeyondTrust remote-support session.
#
# What this does:
#   1. Creates a "Heidelberg Remote Support" shortcut on the Administrator Desktop
#      pointing to https://remote.heidelbergengineering.com
#   2. Disables RDP idle/disconnect auto-logoff (so a long install doesn't kick you out)
#   3. Prevents Windows Update from auto-rebooting while someone is logged in
#
# Run via:
#   cat scripts/appway-windows/heyex_prep_bt_session.ps1 | python3 scripts/ssm_run.py --instance heyex2 -
#
# Revert after session:
#   cat scripts/appway-windows/heyex_revert_bt_session.ps1 | python3 scripts/ssm_run.py --instance heyex2 -

$sep = "=" * 50

# ── 1. Desktop shortcut → BeyondTrust portal ──────────────────────────────────
Write-Host $sep
Write-Host "1. Creating Desktop shortcut: Heidelberg Remote Support..."

$desktopPath = "C:\Users\Administrator\Desktop"
$shortcutPath = Join-Path $desktopPath "Heidelberg Remote Support.url"

$urlContent = @"
[InternetShortcut]
URL=https://remote.heidelbergengineering.com
IconIndex=0
"@

$urlContent | Set-Content -Path $shortcutPath -Encoding ASCII
if (Test-Path $shortcutPath) {
    Write-Host "   Created: $shortcutPath  [OK]"
} else {
    Write-Host "   FAILED to create shortcut  [ERROR]"
}

# ── 2. Disable RDP idle/disconnect timeout ────────────────────────────────────
Write-Host $sep
Write-Host "2. Disabling RDP idle and disconnect timeouts..."

$tsPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
if (-not (Test-Path $tsPath)) {
    New-Item -Path $tsPath -Force | Out-Null
}
# MaxIdleTime = 0 means no idle timeout
Set-ItemProperty -Path $tsPath -Name "MaxIdleTime"          -Value 0 -Type DWord
# MaxDisconnectionTime = 0 means disconnected sessions never expire
Set-ItemProperty -Path $tsPath -Name "MaxDisconnectionTime" -Value 0 -Type DWord
Write-Host "   MaxIdleTime=0  MaxDisconnectionTime=0  [OK]"

# ── 3. Block Windows auto-reboot while user is logged in ─────────────────────
Write-Host $sep
Write-Host "3. Blocking Windows Update auto-reboot during session..."

$wuPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
if (-not (Test-Path $wuPath)) {
    New-Item -Path $wuPath -Force | Out-Null
}
Set-ItemProperty -Path $wuPath -Name "NoAutoRebootWithLoggedOnUsers" -Value 1 -Type DWord
Write-Host "   NoAutoRebootWithLoggedOnUsers=1  [OK]"

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host $sep
Write-Host "Prep complete. EC2 is ready for the Heidelberg BeyondTrust session."
Write-Host ""
Write-Host "   Desktop shortcut:  'Heidelberg Remote Support' -> https://remote.heidelbergengineering.com"
Write-Host "   RDP timeouts:      disabled (no auto-logoff)"
Write-Host "   Windows auto-reboot: blocked while logged in"
Write-Host ""
Write-Host "NEXT STEPS FOR YOU:"
Write-Host "  1. Create an AMI snapshot in the AWS Console (your rollback safety net)"
Write-Host "  2. Tomorrow: run ./scripts/rdp-heyex.sh to RDP in"
Write-Host "  3. When Rouven says 'go': click the 'Heidelberg Remote Support' shortcut on the Desktop"
Write-Host "  4. Join the Teams meeting (audio/video): https://teams.microsoft.com/meet/34938919077733"
