# Heidelberg Remote Support Session — Cheat Sheet

This document covers everything you need to successfully host a Heidelberg Engineering
remote-support session on your **HEYEX 2 EC2** (`Heyex2-testing`, `i-02a7dd1797d85a099`).

Heidelberg uses **BeyondTrust** (formerly Bomgar) for remote sessions. Their technician
takes control of the EC2 desktop via a browser-based client that you download on the EC2.

---

## ✅ Morning-of checklist (before the Teams call starts)

### 1. On your local Ubuntu PC
- [ ] Ubuntu PC is on and logged in
- [ ] Marx Crypto Box CBU dongle is plugged in (LED = red)
- [ ] VirtualHere server running: `sudo systemctl status virtualhere`
- [ ] Tailscale running: `tailscale status` shows `100.97.57.68` active

### 2. Start the EC2 (if it was stopped)
- [ ] AWS Console → EC2 → `Heyex2-testing` → **Start instance**
- [ ] Wait ~2 min until Status checks = 2/2

### 3. RDP into the EC2
```bash
./scripts/rdp-heyex.sh
```
- [ ] RDP window opens
- [ ] Wait ~10 s for the VirtualHere client to appear in system tray
- [ ] VH client shows: `USB CrypToken (In use by you)` ← blue-highlighted
- [ ] If dongle not showing: right-click Heidelberg License Manager tray icon → click "Marx Crypto Box CBU"

### 4. Verify HEYEX 2 is healthy
- [ ] Double-click `HEYEX` shortcut on the Desktop (or Public Desktop)
- [ ] Login: `sysadmin` / `hesmc` → HEYEX 2 main screen opens (no license error)
- [ ] Close HEYEX 2 (leave it closed for the session — Heidelberg will open it)

### 5. Pre-session health check (optional, from this repo)
```bash
cat scripts/appway-windows/heyex_healthcheck.ps1 | python3 scripts/ssm_run.py --instance heyex2 -
```
All items should show `[OK]`.

---

## 🖥️ During the session

### Starting the BeyondTrust session

1. **Inside your RDP window**, double-click the **"Heidelberg Remote Support"** shortcut
   on the Desktop — it opens `https://remote.heidelbergengineering.com` in Internet Explorer.

2. On that page, click **Rouven's name** (or the technician's name who is expecting you).

3. A file `bomgar-scc.exe` (or similar) downloads — **Run** it.

4. BeyondTrust opens a session — Rouven can now see and control the EC2 desktop.

5. Rouven can copy/paste via clipboard (works bidirectionally in BeyondTrust).

6. **Join the Teams call** for audio (Teams link is in Rouven's email):
   ```
   https://teams.microsoft.com/meet/34938919077733
   ```

### During the install — what to expect

- Rouven will install their **AppWay plugin** inside HEYEX 2 (lives in `C:\HEYEX\plugins\`)
- He may restart services (`HELICSVC`, SQL Anywhere, etc.) — this is normal
- He may ask you to verify the dongle / license — if so, right-click VH tray → confirm
- He may reboot the EC2 — if so, after reboot:
  - Wait ~2 min for it to come back
  - Re-run `./scripts/rdp-heyex.sh`
  - VH client auto-starts at logon

### Things to watch for

| What you see | What to do |
|---|---|
| BeyondTrust session drops | Wait 30 s, refresh the page, re-click Rouven's name |
| HEYEX 2 crash / black screen | Tell Rouven, he'll handle it |
| EC2 asks for reboot | Allow it, then re-RDP with `./scripts/rdp-heyex.sh` |
| "No valid license" in HEYEX | Right-click HELICSVC tray → click Marx Crypto Box CBU |
| VH dongle disappears | `Restart-Service HELICSVC` in PowerShell |

---

## 🧹 After the session

### 1. Verify HEYEX 2 + plugin work
- [ ] Launch HEYEX 2 → login `sysadmin` / `hesmc`
- [ ] Navigate to where the new AppWay plugin should appear
- [ ] Test with a sample image from `\\tsclient\heyex-images\`

### 2. Uninstall BeyondTrust client (usually auto-removes, but verify)
- [ ] Windows Settings → Apps → look for "BeyondTrust Support Customer Client" or "Bomgar"
- [ ] If found → Uninstall

### 3. Revert the prep tweaks (re-enable normal RDP timeouts + Windows auto-reboot)
```bash
cat scripts/appway-windows/heyex_revert_bt_session.ps1 | python3 scripts/ssm_run.py --instance heyex2 -
```

### 4. Take a post-session AMI snapshot (recommended)
AWS Console → EC2 → `Heyex2-testing` → Actions → Image and templates →
**Create image** → name: `heyex2-post-heidelberg-install-YYYY-MM-DD`

### 5. (Optional) Rotate the Administrator password
If Rouven may have seen the password during your RDP session:
```bash
# Generate new password and set it via SSM
cat << 'EOF' | python3 scripts/ssm_run.py --instance heyex2 -
$newpw = [System.Web.Security.Membership]::GeneratePassword(20, 4)
net user Administrator "$newpw"
Write-Host "New password: $newpw"
EOF
```
Then update `~/.ssh/AppWay.pem` decrypt step (or just use AWS Console to get the updated password
next time — note: once you change it manually, `aws ec2 get-password-data` won't return it).

---

## ℹ️ Key facts for the session

| | |
|---|---|
| EC2 name | `Heyex2-testing` |
| EC2 public IP | `54.154.242.69` (fixed Elastic IP) |
| HEYEX 2 install dir | `C:\HEYEX\` |
| HEYEX 2 login | `sysadmin` / `hesmc` |
| BeyondTrust portal | https://remote.heidelbergengineering.com |
| Teams meeting | https://teams.microsoft.com/meet/34938919077733 |
| Teams passcode | `E3Xb3rK7` |
| Heidelberg contact | Rouven |
| Desktop shortcut | "Heidelberg Remote Support" (opens BT portal) |

---

## 🔧 Prep / revert scripts (from this repo)

| Script | What it does |
|---|---|
| `scripts/appway-windows/heyex_healthcheck.ps1` | Pre-session health check (all green = good to go) |
| `scripts/appway-windows/heyex_prep_bt_session.ps1` | Applies session tweaks (BT shortcut, RDP timeout off, reboot guard) |
| `scripts/appway-windows/heyex_revert_bt_session.ps1` | Reverts all tweaks after session |

Run any of them via:
```bash
cat scripts/appway-windows/<script>.ps1 | python3 scripts/ssm_run.py --instance heyex2 -
```

> **Full dongle/VH docs:** `docs/heyex2-ubuntu-dongle.md`  
> **Daily usage cheat sheet:** `docs/heyex-daily.md`
