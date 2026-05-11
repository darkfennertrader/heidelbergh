# heyex_revert_bt_session.ps1
# Reverts all changes made by heyex_prep_bt_session.ps1 after the Heidelberg session.
#
# What this reverts:
#   1. Removes the "Heidelberg Remote Support" Desktop shortcut
#   2. Re-enables RDP idle/disconnect timeouts (removes the policy = back to Windows defaults)
#   3. Re-enables Windows Update auto-reboot (removes the block policy)
#
# Run via:
#   cat scripts/appway-windows/heyex_revert_bt_session.ps1 | python3 scripts/ssm_run.py --instance heyex2 -

$sep = "=" * 50

# ── 1. Remove Desktop shortcut ────────────────────────────────────────────────
Write-Host $sep
Write-Host "1. Removing 'Heidelberg Remote Support' Desktop shortcut..."
$shortcutPath = "C:\Users\Administrator\Desktop\Heidelberg Remote Support.url"
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "   Removed: $shortcutPath  [OK]"
} else {
    Write-Host "   Not found (already removed or never created)  [OK]"
}

# ── 2. Re-enable RDP idle/disconnect timeouts (remove policy = back to default) ─
Write-Host $sep
Write-Host "2. Restoring RDP idle and disconnect timeouts to Windows defaults..."
$tsPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
if (Test-Path $tsPath) {
    Remove-ItemProperty -Path $tsPath -Name "MaxIdleTime"          -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $tsPath -Name "MaxDisconnectionTime" -ErrorAction SilentlyContinue
    Write-Host "   Removed MaxIdleTime and MaxDisconnectionTime policy overrides  [OK]"
} else {
    Write-Host "   Policy key not found (nothing to revert)  [OK]"
}

# ── 3. Re-enable Windows Update auto-reboot ───────────────────────────────────
Write-Host $sep
Write-Host "3. Re-enabling Windows Update auto-reboot..."
$wuPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
if (Test-Path $wuPath) {
    Remove-ItemProperty -Path $wuPath -Name "NoAutoRebootWithLoggedOnUsers" -ErrorAction SilentlyContinue
    Write-Host "   Removed NoAutoRebootWithLoggedOnUsers policy override  [OK]"
} else {
    Write-Host "   Policy key not found (nothing to revert)  [OK]"
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host $sep
Write-Host "Revert complete. EC2 is back to normal post-session settings."
Write-Host ""
Write-Host "Reminder — post-session tasks:"
Write-Host "  1. Verify HEYEX 2 + new plugin work (login: sysadmin / hesmc)"
Write-Host "  2. Uninstall BeyondTrust client if still installed (Settings -> Apps)"
Write-Host "  3. Create a post-session AMI snapshot in AWS Console"
Write-Host "  4. (Optional) Rotate Administrator password"
