 # HEYEX 2 Dongle Bridge — Marx CryptoBox CBU via Tailscale + VirtualHere

This document describes how the physical **Marx Crypto Box CBU** USB license dongle
(supplied by Heidelberg Engineering) is bridged from a local Windows 11 PC to the
cloud-hosted **Heyex2-testing** EC2 (`i-02a7dd1797d85a099`, Windows Server 2019)
using **Tailscale** (private mesh VPN) and **VirtualHere** (USB-over-IP).

This is required because the EC2 has no physical USB port, but HEYEX 2 v2.6.10
requires the dongle to be locally attached in order to activate its license.

---

## Architecture

```
Local Windows 11 PC (Raimondo)          EC2: Heyex2-testing (AWS eu-west-1)
┌─────────────────────────────┐         ┌────────────────────────────────────┐
│  Marx Crypto Box CBU        │         │  VirtualHere Client v5.9.8         │
│  USB port → red LED (idle)  │         │  (vhui64.exe, Session 2 / RDP)     │
│  Driver: CBUSetup_Oct2025   │         │  Config: vhui.ini                  │
│                             │   TCP   │  Server: 100.64.25.24:7575         │
│  VirtualHere USB Server     │◄───────►│  Autouse: USB CrypToken            │
│  (vhusbdwin64.exe, service) │ 7575    │                                    │
│  Tailscale IP: 100.64.25.24 │         │  Tailscale IP: 100.79.248.90       │
│  Firewall: TCP 7575 open    │         │  Marx driver: cbu2_64.inf (oem10)  │
└─────────────────────────────┘         │              cbusb_64.inf (oem8)   │
                                        │                                    │
                                        │  HELICSVC: Heidelberg Eye Explorer │
                                        │  License Manager — reads dongle    │
                                        └────────────────────────────────────┘
```

---

## Component Details

### Local PC (USB server side)

| Item | Detail |
|------|--------|
| OS | Windows 11 Pro |
| Tailscale IP | `100.64.25.24` |
| VirtualHere Server | `C:\VirtualHere\vhusbdwin64.exe` — runs as Windows service `VirtualHere USB Server` |
| Marx driver | `CBUSetup_13Oct2025.zip` from https://www.marx.com/downloads/drivers-and-diagnostic/cbusetup/CBUSetup_13Oct2025.zip |
| Dongle | Marx Crypto Box CBU — LED: **red** = idle/ready, **green** = HEYEX 2 actively reading |
| Firewall | Inbound TCP 7575 open |

### EC2 (USB client side)

| Item | Detail |
|------|--------|
| Instance | `i-02a7dd1797d85a099` — Heyex2-testing |
| Tailscale IP | `100.79.248.90` |
| VirtualHere Client | `C:\VirtualHere\vhui64.exe` v5.9.8 |
| VH config file | `C:\Users\Administrator\AppData\Roaming\vhui.ini` (**not** `client.ini`) |
| Marx drivers | `oem8.inf` (cbusb_64.inf) + `oem10.inf` (cbu2_64.inf) — installed by HEYEX 2 setup |
| Device (when forwarded) | `CBUSB Ver 2.0`, VID_0D7A&PID_0001, Status: OK |
| License Manager service | `HELICSVC` — Heidelberg Eye Explorer License Manager |

---

## VirtualHere Config (`vhui.ini`)

Location on EC2: `C:\Users\Administrator\AppData\Roaming\vhui.ini`

```ini
[SERVERS]
SERVER=100.64.25.24:7575

[AUTOUSE]
USB CrypToken=1
```

> **Important**: VirtualHere Client 5.x on Windows uses `vhui.ini` — NOT `client.ini`.
> Earlier attempts used `client.ini` which was silently ignored.

---

## How to Start a Session (SOP)

### Prerequisites
- Local PC is on and Tailscale is running (`tailscale status` shows `100.64.25.24 msi`)
- Marx dongle is plugged into the local PC (LED = red)
- VirtualHere USB Server service is running on local PC
- EC2 `Heyex2-testing` is started and SSM is Online

### Steps

1. **RDP into the EC2**: `54.154.242.69` (Elastic IP — fixed, does not change on stop/start) → user `Administrator`

2. **Launch VirtualHere client** in your RDP session:
   ```
   C:\VirtualHere\vhui64.exe
   ```
   The client will auto-connect to `100.64.25.24:7575` and auto-use `USB CrypToken`.

3. **Verify in VH client tree**: You should see:
   ```
   USB Servers
   └─ Windows Hub (green dot)
      └─ USB CrypToken (In use by you)   ← highlighted blue
   ```

4. **Restart License Manager** (if HEYEX 2 was already running, or after EC2 reboot):
   ```powershell
   Restart-Service HELICSVC
   ```
   Or via SSM from the backend EC2:
   ```bash
   aws ssm send-command --instance-ids i-02a7dd1797d85a099 \
     --document-name AWS-RunPowerShellScript \
     --parameters 'commands=["Restart-Service HELICSVC"]' \
     --region eu-west-1
   ```

5. **Activate license via License Manager UI**:
   - Look in system tray (bottom-right) for the Heidelberg Eye Explorer License Manager icon
   - Click it → click the **"Marx Crypto Box CBU"** image
   - Screen refreshes showing active license

6. **Launch HEYEX 2** from the desktop or Start menu
   - Login: `sysadmin` / `hesmc`

---

## Troubleshooting

### VH client shows "USB Servers" but nothing under it (empty tree)
**Cause**: VH client launched in Session 0 (SSM/system session) instead of the
interactive RDP session. Session 0 cannot forward USB devices.

**Fix**: Make sure you launch `vhui64.exe` directly in your RDP session (double-click
the exe, or use the scheduled task). Do NOT rely on SSM's `Start-Process` to launch it.

### VH client tree completely empty (no "Windows Hub" entry)
**Cause**: Client is not connecting to the server — either Tailscale is down, firewall
blocking port 7575, or the VH server service stopped on the local PC.

**Check**:
```powershell
# On EC2 (via SSM):
Test-NetConnection -ComputerName 100.64.25.24 -Port 7575
# Should return TcpTestSucceeded: True
```

**Fix**:
- Restart Tailscale on local PC
- Restart VirtualHere USB Server service on local PC (`services.msc`)
- Check Windows Firewall on local PC — inbound TCP 7575 must be open

### Dongle LED stays red (not green) after HEYEX 2 starts
This is normal during HEYEX 2 startup. The LED turns green when HEYEX 2 actively
queries the dongle for license info (typically within 10–30 seconds of login).

### HEYEX 2 shows "No valid license" or license expired
1. Check dongle is still "In use by you" in VH client
2. `Restart-Service HELICSVC`
3. In License Manager tray → click "Marx Crypto Box CBU" again

### After EC2 reboot — VH client not auto-starting in user session
The scheduled task `VirtualHereClient` is configured to run at logon for Administrator.
It should start automatically when you RDP in. If it doesn't:
1. Check Task Scheduler → VirtualHereClient → Last Run Result
2. Manually run: `C:\VirtualHere\vhui64.exe`

---

## Scheduled Task for VH Client (Auto-start at RDP logon)

Created via SSM. Task name: `VirtualHereClient`

```
Trigger:    At log on of Administrator
Action:     C:\VirtualHere\vhui64.exe
Run Level:  Highest
```

On each RDP login, this launches `vhui64.exe` in the interactive session,
which reads `vhui.ini`, connects to `100.64.25.24:7575`, and auto-uses the dongle.

---

## One-Time Setup: Auto-Use Device (persistent)

This is a one-time action per EC2 that makes the VH client automatically re-claim the dongle after **any** disconnect — HELICSVC restart, HEYEX 2 restart, short network blip — without any manual click.

### Steps (in your RDP session on the EC2)

1. Open the VirtualHere client window (`C:\VirtualHere\vhui64.exe`)
2. Expand **`USB Servers → Windows Hub`**
3. **Right-click `USB CrypToken`** → click **"Auto-Use Device"**
   - A check mark appears next to the menu item — the rule is now active and persisted by VH
4. That's it. The rule survives EC2 reboots.

> **Why this is better than `[AUTOUSE]` in `vhui.ini`**: The ini-level autouse fires only at VH client startup. The per-device GUI rule fires whenever the device becomes available (startup **or** reconnect), so it re-claims the dongle even if it temporarily drops while the client is already running.

---

## Key Lessons Learned

1. **Wrong vendor investigated**: The dongle is a **Marx CryptoBox CBU** (not Thales/Sentinel HASP).
   Confirmed by Windows device name "USB CrypToken" and Klaus Brönnimann (Heidelberg) email.
   The correct driver is `CBUSetup.exe` from **marx.com/en/support/downloads**, not `HASPUserSetup.exe`.

2. **Driver already on EC2**: The HEYEX 2 installer bundles the Marx CBU driver
   (`cbu2_64.inf`, `cbusb_64.inf`) — no separate install needed on the machine running HEYEX 2.

3. **Driver needed on local PC**: VirtualHere Server requires the USB device to be
   properly enumerated by Windows (not just showing as "Other devices" yellow triangle).
   Installing `CBUSetup.exe` on the local PC was required.

4. **VH Client 5.x uses `vhui.ini`**: Earlier versions used `client.ini`.
   All manual config written to `client.ini` was silently ignored.

5. **Session 0 isolation**: SSM runs in Session 0; GUI apps launched via SSM's
   `Start-Process` appear in Session 0 and cannot forward USB devices or display
   windows in the user's RDP session (Session 2). Must use scheduled tasks with
   `LogonType = Interactive` to launch in the user session.

6. **`Specify USB Server...` menu item**: The correct way to add the hub manually
   in the VH client GUI is: right-click the "USB Servers" node → Specify USB Server…
   → enter `100.64.25.24` (port 7575).

7. **`Auto-Use Device` (GUI, per-device) beats `[AUTOUSE]` in vhui.ini**: The per-device GUI
   rule is persistent and re-claims the dongle after any disconnect (HELICSVC restart, network
   blip, HEYEX 2 restart). The ini-level `[AUTOUSE] USB CrypToken=1` only fires at VH client
   startup — it doesn't help if the dongle bounces while the client is already running.

8. **Elastic IP `54.154.242.69`**: The EC2 has a fixed Elastic IP — the public address does
   **not** change when the instance is stopped and restarted. Always RDP to `54.154.242.69`.

---

## Cold-Start Steps (from everything off)

### On your local Windows 11 PC

1. **Turn on the PC** and log in.
2. **Plug in the Marx Crypto Box CBU dongle** — LED should go **red** (ready).
3. **Verify Tailscale is running** — look for the Tailscale icon in the system tray (bottom-right). If it's not there, open Tailscale from the Start menu and connect. Once signed in it auto-starts on boot.
4. **Verify VirtualHere USB Server is running** — it's a Windows service that auto-starts on boot. To confirm: `Win+R` → `services.msc` → find **"VirtualHere USB Server"** → Status = **Running**. If not: right-click → **Start**.

### Start the EC2

5. Go to **AWS Console → EC2 → Instances → `Heyex2-testing`** → **Instance state → Start instance**.
6. Wait ~2 minutes until **Instance State = Running** and **Status checks = 2/2 passed**.
7. The Public IP is **always `54.154.242.69`** (Elastic IP — it does not change on stop/start).

### RDP in and bring the dongle online

8. **RDP into the EC2**: open your RDP client → connect to `54.154.242.69` → user `Administrator` → password (get it from EC2 Console → Connect → RDP client → Get password → paste `AppWay.pem`).
9. Wait ~10 seconds after logging in. The scheduled task `VirtualHereClient` automatically launches `C:\VirtualHere\vhui64.exe` in your RDP session.
10. **Check the VirtualHere client window** — under `USB Servers → Windows Hub` you should see **"USB CrypToken (In use by you)"** (highlighted blue). This happens automatically via the **Auto-Use Device** rule (see [One-Time Setup: Auto-Use Device](#one-time-setup-auto-use-device-persistent) below).
    - If it's **not there**: right-click **`USB Servers`** → **Specify USB Server…** → enter `100.64.25.24`, port `7575` → OK. Then right-click the dongle entry → **Use this device**. (Only needed on a fresh EC2 before the Auto-Use rule has been set.)
11. **Restart the License Manager** — open PowerShell as Administrator and run:
    ```powershell
    Restart-Service HELICSVC
    ```
12. **Activate the license** — look in the system tray (bottom-right, click `^` to expand). Click the **Heidelberg Eye Explorer License Manager** icon → click the **"Marx Crypto Box CBU"** image → the screen refreshes showing the active license.
13. **Launch HEYEX 2** from the desktop or Start menu → login: `sysadmin` / `hesmc`.

### Shutdown (when done)

14. Close HEYEX 2.
15. In AWS Console → EC2 → Heyex2-testing → **Instance state → Stop instance** (preserves data, saves cost).
16. On your local PC: leave the dongle plugged in or unplug — either is fine. Tailscale and VirtualHere Server can stay running (near-zero resource use).

---

## Verified

- **2026-05-09 21:38 UTC** — Full end-to-end dongle bridge working:
  - Local PC: Marx CBU driver installed, VH Server running, dongle LED = red (ready)
  - EC2: VH Client v5.9.8 in RDP session, `USB CrypToken (In use by you)`
  - EC2 Device Manager: `CBUSB Ver 2.0`, Status `OK`, `VID_0D7A&PID_0001`
  - `HELICSVC` restarted; License Manager showed active Marx Crypto Box CBU license
  - HEYEX 2 v2.6.10 launched successfully, logged in as `sysadmin`/`hesmc` ✓

- **2026-05-10** — Auto-Use Device rule set + Elastic IP confirmed:
  - Right-clicked `USB CrypToken` in VH client → **Auto-Use Device** ✓ (check mark visible)
  - EC2 Elastic IP confirmed as `54.154.242.69` (fixed, no longer changes on stop/start)
  - **Persistence verified**: `HELICSVC` restarted via SSM → `USB CrypToken (In use by you)` stayed
    highlighted blue with **no manual click** — Auto-Use Device rule re-claimed dongle automatically ✓
