# Klaus ProcMon Runbook — `ServiceAISolutionAutomaticCheckSleepTimeInMinutes` Overwrite

**Goal:** Capture the exact call site inside `MCAISolutionService.exe` (or any DLL it loads)
that resets `ServiceAISolutionAutomaticCheckSleepTimeInMinutes` to `20` every time the service
starts — providing enough evidence for Heidelberg to issue a permanent fix.

---

## Background

| Item | Value |
|------|-------|
| Registry path | `HKLM\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution` |
| Value name | `ServiceAISolutionAutomaticCheckSleepTimeInMinutes` |
| Type | `DWORD` |
| Desired value | `1` (poll interval 1 min) |
| Overwritten to | `20` within ~5 s of `Start-Service` |
| Service display name | `MedicalCommunications AI Solution Service` |
| Executable | `MCAISolutionService.exe` v1.2.2031.0 (32-bit) |

> ⚠️ **Why `WOW6432Node`?** `MCAISolutionService.exe` is a **32-bit** binary.
> Windows automatically redirects 32-bit registry writes to
> `HKLM\SOFTWARE\WOW6432Node\…`. If you navigate to the *plain*
> `HKLM\SOFTWARE\MedicalCommunications\…` path you will **not** find this value.

---

## Part 1 — Set the registry value to 1 via regedit (GUI)

1. On the AppWay EC2 desktop, press **Win + R**, type `regedit`, press **Enter**.
   Accept the UAC prompt if it appears.

2. In the regedit address bar at the top, paste the path below and press **Enter**:

   ```
   HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\MedicalCommunications\AISolution
   ```

3. In the right pane, locate **`ServiceAISolutionAutomaticCheckSleepTimeInMinutes`**
   and double-click it.

4. In the dialog:
   - **Base**: select **Decimal**
   - **Value data**: type `1`
   - Click **OK**

5. The value now reads `1`. Leave regedit open so you can confirm the overwrite during the test.

---

## Part 2 — Stop the service before arming ProcMon

Running ProcMon *after* the service has already started means the overwrite event has
already been missed. Stop the service first:

**Option A — services.msc (GUI)**
1. Press **Win + R** → `services.msc` → Enter.
2. Scroll to **"MedicalCommunications AI Solution Service"**.
3. Right-click → **Stop**.

**Option B — Admin PowerShell**
```powershell
Stop-Service "MedicalCommunications AI Solution Service"
```

Confirm the service status is **Stopped** before continuing.

---

## Part 3 — Configure Process Monitor

> Download Sysinternals Process Monitor from:
> <https://learn.microsoft.com/en-us/sysinternals/downloads/procmon>
> (or copy `Procmon64.exe` from a USB / network share)

1. Launch **Procmon64.exe** as Administrator (right-click → Run as administrator).

2. **Configure symbol resolution** (so stack traces show function names):
   - Menu: **Options → Configure Symbols…**
   - Set `Dbghelp.dll path` to:
     ```
     C:\Windows\System32\dbghelp.dll
     ```
   - Set `Symbol path` to:
     ```
     srv*C:\Symbols*https://msdl.microsoft.com/download/symbols
     ```
     (Create `C:\Symbols` if it does not exist.)
   - Click **OK**.

3. **Set filters** — Menu: **Filter → Filter…** (Ctrl+L):

   | Column | Relation | Value | Action |
   |--------|----------|-------|--------|
   | Path | contains | `ServiceAISolutionAutomaticCheckSleepTimeInMinutes` | **Include** |
   | Operation | is | `RegSetValue` | **Include** |

   Click **Add** after each row, then **OK**.
   
   > Optionally also add `RegOpenKey` / `RegQueryValue` with the same path to see the
   > full access sequence, but `RegSetValue` is the critical one.

4. **Drop filtered events** to keep the trace small:
   - Menu: **Filter → Drop Filtered Events** — ensure this is **checked**.

5. **Pause capture** for now:
   - Press **Ctrl + E** (or toolbar magnifying-glass button) so that the button
     shows as "not capturing". We will un-pause just before starting the service.

---

## Part 4 — Capture the overwrite event

1. Switch back to Process Monitor and press **Ctrl + E** to **start capturing**.

2. Immediately start the service:
   - **services.msc**: right-click → Start  
   — or —
   - Admin PowerShell: `Start-Service "MedicalCommunications AI Solution Service"`

3. **Wait ~10 seconds**. Within 5 s you should see one or two `RegSetValue` rows
   appear in ProcMon with `Data: 20`.

4. Press **Ctrl + E** again to **stop capturing**.

---

## Part 5 — Examine the stack trace

1. Double-click the `RegSetValue` row (or right-click → **Properties**).
2. Select the **Stack** tab.
3. The stack frames list the exact module and function that issued the write.
   Look for a frame inside `MCAISolutionService.exe` or any
   `MedicalCommunications*.dll` — that is the offending call site.
4. Right-click any frame → **Copy All** to copy the full stack to clipboard,
   then paste into a text file / email for the Heidelberg team.

---

## Part 6 — Save the trace file

**File → Save…**
- Format: **PML** (native Process Monitor Log — preserves full stacks)
- Suggested filename: `procmon-aisolution-overwrite-<YYYYMMDD>.PML`

Share the `.PML` file with the AppWay team so the trace can be re-opened and
re-inspected without re-running the test.

---

## Appendix — Quick reference

| Step | GUI shortcut |
|------|-------------|
| Open regedit | Win + R → `regedit` |
| Open services | Win + R → `services.msc` |
| ProcMon filter | Ctrl + L |
| Toggle capture | Ctrl + E |
| Save trace | Ctrl + S |

### Expected ProcMon row

```
Time       Process Name        PID   Operation    Path                                          Result  Detail
hh:mm:ss   MCAISolutionSer…    XXXX  RegSetValue  HKLM\SOFTWARE\Wow6432Node\MedicalCom…\…SleepTimeInMinutes  SUCCESS  Type: REG_DWORD, Data: 0x14
```

`0x14` = **20 decimal** — the unwanted overwrite.

---

*Runbook prepared for Klaus Heidelberg diagnostic session, AppWay EC2 `i-02a99abeba370f0a7` (eu-west-1).*
