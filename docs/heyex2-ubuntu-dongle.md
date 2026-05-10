# HEYEX 2 Dongle Bridge — Marx CryptoBox CBU via Tailscale + VirtualHere (Ubuntu)

This document describes how the physical **Marx Crypto Box CBU** USB license dongle
(supplied by Heidelberg Engineering) is bridged from a **local Ubuntu 24.04 PC** to the
cloud-hosted **Heyex2-testing** EC2 (`i-02a7dd1797d85a099`, Windows Server 2019)
using **Tailscale** (private mesh VPN) and **VirtualHere** (USB-over-IP).

This is required because the EC2 has no physical USB port, but HEYEX 2 v2.6.10
requires the dongle to be locally attached in order to activate its license.

> **Note**: For the legacy Windows-based setup, see `docs/heyex2-windows-dongle.md`.

---

## Architecture

```
Local Ubuntu 24.04 PC (ai-dev)          EC2: Heyex2-testing (AWS eu-west-1)
┌─────────────────────────────┐         ┌────────────────────────────────────┐
│  Marx Crypto Box CBU        │         │  VirtualHere Client v5.9.8         │
│  USB port → red LED (idle)  │         │  (vhui64.exe, Session 2 / RDP)     │
│  No driver needed on Linux  │         │  Config: vhui.ini                  │
│                             │   TCP   │  Server: 100.97.57.68:7575         │
│  VirtualHere USB Server     │◄───────►│  Autouse: USB CrypToken            │
│  /usr/local/sbin/vhusbdx86_64│ 7575   │                                    │
│  systemd: virtualhere.service│        │  Tailscale IP: 100.79.248.90       │
│  Tailscale IP: 100.97.57.68 │         │  Marx driver: cbu2_64.inf (oem10)  │
│  No extra firewall needed   │         │              cbusb_64.inf (oem8)   │
└─────────────────────────────┘         │                                    │
                                        │  HELICSVC: Heidelberg Eye Explorer │
                                        │  License Manager — reads dongle    │
                                        └────────────────────────────────────┘
```

---

## Component Details

### Local Ubuntu PC (USB server side)

| Item | Detail |
|------|--------|
| OS | Ubuntu 24.04.4 LTS x86_64 |
| Hostname | `ai-dev` |
| Tailscale IP | `100.97.57.68` |
| VirtualHere Server binary | `/usr/local/sbin/vhusbdx86_64` |
| VH config file | `/usr/local/etc/virtualhere/config.ini` (auto-generated defaults) |
| systemd service | `virtualhere.service` — enabled, starts on boot |
| Marx driver | **Not needed on Linux** — kernel enumerates USB generically via `usbfs`; driver only needed on EC2 (already installed by HEYEX 2 setup) |
| Dongle | Marx Crypto Box CBU — LED: **red** = idle/ready, **green** = HEYEX 2 actively reading |
| Firewall | No extra rules needed — Tailscale interface is already trusted |

### EC2 (USB client side)

| Item | Detail |
|------|--------|
| Instance | `i-02a7dd1797d85a099` — Heyex2-testing |
| Public IP | `54.154.242.69` (Elastic IP — **fixed**, does not change on stop/start) |
| Tailscale IP | `100.79.248.90` |
| VirtualHere Client | `C:\VirtualHere\vhui64.exe` v5.9.8 |
| VH config file | `C:\Users\Administrator\AppData\Roaming\vhui.ini` |
| Marx drivers | `oem8.inf` (cbusb_64.inf) + `oem10.inf` (cbu2_64.inf) — installed by HEYEX 2 setup |
| Device (when forwarded) | `CBUSB Ver 2.0`, VID_0D7A&PID_0001, Status: OK |
| License Manager service | `HELICSVC` — Heidelberg Eye Explorer License Manager |

---

## Installing VirtualHere USB Server on Ubuntu (one-time)

```bash
# Install via official script (detects arch automatically, creates systemd unit)
curl -fsSL https://raw.githubusercontent.com/virtualhere/script/master/install_server | sudo sh

# Verify it's running
sudo systemctl status virtualhere --no-pager

# Verify it's listening on port 7575
sudo ss -tlnp | grep 7575

# Verify the dongle is visible (after plugging in)
lsusb | grep -i 0d7a
# Expected: Bus 00X Device 0XX: ID 0d7a:0001 MARX Datentechnik GmbH CrypToken
```

The installer:
- Downloads `/usr/local/sbin/vhusbdx86_64`
- Creates `/etc/systemd/system/virtualhere.service`
- Creates `/usr/local/etc/virtualhere/config.ini` with sensible defaults
- Enables + starts the service on boot

---

## VirtualHere Config (`vhui.ini`) on EC2

Location: `C:\Users\Administrator\AppData\Roaming\vhui.ini`

```ini
[SERVERS]
SERVER=100.97.57.68:7575

[AUTOUSE]
USB CrypToken=1
```

> **Important**: VirtualHere Client 5.x on Windows uses `vhui.ini` — NOT `client.ini`.

---

## How to Start a Session (SOP)

### Prerequisites
- Ubuntu PC is on and logged in
- Marx dongle is plugged into the Ubuntu PC (LED = red)
- VirtualHere USB Server service is running (`sudo systemctl status virtualhere`)
- Tailscale is running on the Ubuntu PC (`tailscale status` shows `100.97.57.68`)
- EC2 `Heyex2-testing` is started and SSM is Online

### Steps

1. **RDP into the EC2**: `54.154.242.69` (Elastic IP — fixed) → user `Administrator`

2. **VirtualHere client auto-starts** in your RDP session via the scheduled task `VirtualHereClient`.
   Wait ~10 seconds for it to appear.

3. **Verify in VH client tree**: You should see:
   ```
   USB Servers
   └─ Linux USB Hub (or similar, green dot)
      └─ USB CrypToken (In use by you)   ← highlighted blue
   ```
   This is automatic via the **Auto-Use Device** rule.

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

## One-Time Setup: Auto-Use Device (persistent)

This is a one-time action per EC2 that makes the VH client automatically re-claim the dongle
after any disconnect — HELICSVC restart, HEYEX 2 restart, short network blip — without any manual click.

### Steps (in your RDP session on the EC2)

1. Open the VirtualHere client window (`C:\VirtualHere\vhui64.exe`)
2. Expand **`USB Servers → Linux USB Hub`**
3. **Right-click `USB CrypToken`** → click **"Auto-Use Device"**
   - A check mark appears next to the menu item — the rule is now active and persisted by VH
4. That's it. The rule survives EC2 reboots.

> **Note on first-time server change**: If you replace the Ubuntu PC or change its Tailscale IP,
> you'll need to:
> 1. Update `vhui.ini` on the EC2 (via SSM or manually in RDP)
> 2. In VH client: right-click the old hub → Remove, then right-click `USB Servers` →
>    **Specify USB Server…** → enter the new IP, port `7575`
> 3. Re-apply **Auto-Use Device** on the dongle entry

---

## Troubleshooting

### VH client tree empty (no hub)
**Cause**: Tailscale is down on either machine, or VH server stopped on Ubuntu PC.

**Check on Ubuntu**:
```bash
tailscale status                        # should show EC2 IP (100.79.248.90)
sudo systemctl status virtualhere       # should be active (running)
sudo journalctl -u virtualhere -n 20    # look for errors
```

**Check connectivity from EC2 (via SSM)**:
```powershell
Test-NetConnection -ComputerName 100.97.57.68 -Port 7575
# Should return TcpTestSucceeded: True
```

**Fix**:
```bash
sudo systemctl restart virtualhere    # restart VH server on Ubuntu
sudo tailscale up                     # reconnect Tailscale if needed
```

### VH client shows hub but no dongle entry
**Cause**: Dongle is not plugged in, or VH server didn't pick it up.

**Check on Ubuntu**:
```bash
lsusb | grep 0d7a          # dongle must appear
sudo journalctl -u virtualhere --since "1 minute ago"   # look for 0d7a:0001 line
```

**Fix**: Unplug and re-plug the dongle. VH server detects hot-plug automatically.

### Dongle LED stays red (not green) after HEYEX 2 starts
Normal during startup. LED turns green when HEYEX 2 actively queries the dongle
(typically within 10–30 seconds of login).

### HEYEX 2 shows "No valid license"
1. Check `USB CrypToken (In use by you)` is still highlighted blue in VH client
2. `Restart-Service HELICSVC`
3. In License Manager tray → click "Marx Crypto Box CBU" image again

### VH client not auto-starting after RDP login
The scheduled task `VirtualHereClient` should start `vhui64.exe` at logon.
If it doesn't:
1. Check Task Scheduler → VirtualHereClient → Last Run Result
2. Manually run: `C:\VirtualHere\vhui64.exe`

---

## Scheduled Task for VH Client (Auto-start at RDP logon)

Task name: `VirtualHereClient`

```
Trigger:    At log on of Administrator
Action:     C:\VirtualHere\vhui64.exe
Run Level:  Highest
```

On each RDP login, this launches `vhui64.exe` in the interactive session,
which reads `vhui.ini`, connects to `100.97.57.68:7575`, and auto-uses the dongle.

---

## Key Notes / Lessons Learned

1. **No Marx driver needed on Linux**: Unlike Windows, Ubuntu (and Linux in general) exposes USB
   devices via `usbfs` without vendor drivers. The VH server runs entirely in userspace and reads
   raw USB data. The Marx CBU driver (`cbu2_64.inf`, `cbusb_64.inf`) is only needed on the EC2
   (the machine running HEYEX 2), and it's already installed there by the HEYEX 2 setup.

2. **VH server runs as root via systemd**: This ensures it has access to all USB devices without
   udev permission issues. Logs go to `journalctl -u virtualhere`.

3. **VH Client 5.x uses `vhui.ini`**: Earlier versions used `client.ini`. All manual config
   written to `client.ini` is silently ignored.

4. **Session 0 isolation on EC2**: SSM runs in Session 0; `vhui64.exe` must be launched in the
   interactive RDP session (Session 2). The scheduled task handles this automatically.

5. **`Auto-Use Device` (GUI, per-device) beats `[AUTOUSE]` in vhui.ini**: The per-device GUI
   rule re-claims the dongle after any disconnect. The ini-level `[AUTOUSE]` only fires at
   VH client startup.

6. **Elastic IP `54.154.242.69`**: The EC2 has a fixed Elastic IP — always RDP to `54.154.242.69`.

7. **Tailscale on Ubuntu auto-starts**: After `sudo tailscale up` and login, Tailscale runs as
   a systemd service (`tailscaled`) and reconnects automatically on boot/resume.

---

## Cold-Start Steps (from everything off)

### On your local Ubuntu PC

1. **Turn on the PC** and log in.
2. **Plug in the Marx Crypto Box CBU dongle** — LED should go **red** (ready).
3. **Verify Tailscale is connected**:
   ```bash
   tailscale status | grep 100.97.57.68
   # Should show: 100.97.57.68  ai-dev  ...  active
   ```
   If not: `sudo tailscale up`
4. **Verify VirtualHere USB Server is running**:
   ```bash
   sudo systemctl status virtualhere --no-pager
   # Should show: Active: active (running)
   ```
   If not: `sudo systemctl start virtualhere`
5. **Verify the dongle is being shared**:
   ```bash
   lsusb | grep 0d7a
   # Should show: 0d7a:0001 MARX Datentechnik GmbH CrypToken
   ```

### Start the EC2

6. Go to **AWS Console → EC2 → Instances → `Heyex2-testing`** → **Instance state → Start instance**.
7. Wait ~2 minutes until **Instance State = Running** and **Status checks = 2/2 passed**.
8. The Public IP is **always `54.154.242.69`** (Elastic IP — does not change on stop/start).

### RDP in and bring the dongle online

9. **RDP into the EC2**: `54.154.242.69` → user `Administrator` → password (EC2 Console → Connect → Get password → paste `AppWay.pem`).
10. Wait ~10 seconds. The scheduled task `VirtualHereClient` auto-launches `vhui64.exe` in your RDP session.
11. **Check the VirtualHere client window** — under `USB Servers → Linux USB Hub` you should see **"USB CrypToken (In use by you)"** (highlighted blue) automatically.
    - If not there: right-click **`USB Servers`** → **Specify USB Server…** → `100.97.57.68`, port `7575` → OK. Then right-click dongle → **Use this device** and **Auto-Use Device**.
12. **Restart the License Manager**:
    ```powershell
    Restart-Service HELICSVC
    ```
13. **Activate the license** — system tray → Heidelberg License Manager icon → click **"Marx Crypto Box CBU"** image.
14. **Launch HEYEX 2** → login: `sysadmin` / `hesmc`.

### Shutdown (when done)

15. Close HEYEX 2.
16. In AWS Console → EC2 → Heyex2-testing → **Instance state → Stop instance**.
17. On your Ubuntu PC: leave dongle plugged in or unplug — either is fine. Both Tailscale and VH server stay running (near-zero resource use).

---

## Loading Local Images into HEYEX 2 (RDP Drive Redirection)

During an RDP session, your local Ubuntu folder is available **live** inside the Windows
EC2 as a network path — no file copying needed. Drop a file on Ubuntu → it appears in
Windows instantly.

### Setup (one-time on your Ubuntu PC)

```bash
# 1. Install xfreerdp (free, open-source RDP client)
sudo apt install -y freerdp2-x11

# 2. Create the local images folder (if not already done)
mkdir -p /home/ray/projects/heyex-test-images
```

### Launching RDP with the folder shared

Use the `scripts/rdp-heyex.sh` script from this repo:

```bash
# Copy once to your local machine, then run:
chmod +x rdp-heyex.sh
./rdp-heyex.sh
```

Or run the `xfreerdp` command directly:

```bash
xfreerdp \
  /v:54.154.242.69 \
  /u:Administrator \
  /size:1600x900 \
  /dynamic-resolution \
  /drive:heyex-images,/home/ray/projects/heyex-test-images \
  /clipboard \
  /cert:ignore
```

### Accessing the folder inside Windows

Once connected, your Ubuntu folder appears as:
- **`\\tsclient\heyex-images\`** — type this in any Windows "Open" / "Browse" dialog
- **Desktop shortcut**: `HEYEX Images (Ubuntu).lnk` (pre-created on the EC2 Desktop)
- **Documents shortcut**: same shortcut in `C:\Users\Administrator\Documents\`

> **Note**: The shortcut points to `\\tsclient\heyex-images\` which only resolves during
> an active RDP session where the drive redirection is enabled. If you open HEYEX 2
> without launching via `rdp-heyex.sh`, the shortcut will not resolve.

### Alternatively — Remmina GUI (same result, no terminal)

1. Open Remmina → right-click your `54.154.242.69` connection → **Edit**
2. Go to the **"Advanced"** tab
3. Find **"Share folder"** → browse to `/home/ray/projects/heyex-test-images`
4. **Save** and connect — Remmina remembers this setting forever
5. Your folder appears as `heyex-test-images on AI-DEV` under **This PC → Redirected drives and folders**

### Workflow

1. Copy/drop your test images into `/home/ray/projects/heyex-test-images/` on Ubuntu
2. In the RDP session → double-click the **"HEYEX Images (Ubuntu)"** shortcut on the Desktop
   (or navigate to `\\tsclient\heyex-images\` in HEYEX 2's file browser)
3. Select your image → HEYEX 2 reads it directly from Ubuntu — no extra copy step

---

## Verified

- **2026-05-10** — Full migration from Windows PC to Ubuntu 24.04 PC:
  - Ubuntu PC (`ai-dev`): Tailscale `100.97.57.68`, VH server `vhusbdx86_64` running, dongle `0d7a:0001` enumerated ✓
  - EC2 `vhui.ini` updated to `100.97.57.68:7575` via SSM ✓
  - EC2 → Ubuntu TCP 7575 connectivity: `TcpTestSucceeded: True` ✓
  - VH client: connected to Linux USB Hub, `USB CrypToken (In use by you)`, Auto-Use Device set ✓
  - `HELICSVC` restarted; License Manager showed active Marx Crypto Box CBU license ✓
  - HEYEX 2 v2.6.10 launched successfully, logged in as `sysadmin`/`hesmc` ✓
