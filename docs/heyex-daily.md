# HEYEX 2 — daily cheat sheet

> Everything you need to use HEYEX 2 from your local Ubuntu PC.  
> No AWS console, no password copying, just one command.

---

## 🖥️ Every time you want to use HEYEX 2

Open a terminal on your **local Ubuntu PC** and run:

```bash
~/projects/appway-backend/scripts/rdp-heyex.sh
```

That's it. The script will:
- Fetch the EC2 Administrator password automatically (no pasting needed)
- Open an RDP window into the `Heyex2-testing` EC2
- Share your local images folder so it appears inside Windows

---

## 📂 Opening a test image in HEYEX 2

1. Copy your `.E2E` / `.dcm` image into this folder on your **Ubuntu PC**:
   ```
   /home/ray/projects/heyex-test-images/
   ```

2. Inside the Windows RDP window, double-click the **"HEYEX Images (Ubuntu)"** shortcut on the Desktop (or in Documents).

3. Open the image in HEYEX 2 from that folder.

That's it — the folder is a live view of your local Ubuntu directory.

---

## 🔴 If something is wrong

| Problem | Fix |
|---|---|
| **Script says "Could not auto-fetch password"** | Your AWS `milani` profile isn't set up. Run `aws configure --profile milani` (needs the Milani access key + secret). Then re-run the script. |
| **RDP says "login failed"** | The EC2 might be stopped. Start it: `aws ec2 start-instances --instance-ids i-02a7dd1797d85a099 --region eu-west-1 --profile milani` — wait 30 s, then re-run the script. |
| **"HEYEX Images (Ubuntu)" shortcut is missing** | Double-click `\\tsclient\heyex-images\` in File Explorer address bar. Or tell Cline to recreate the Desktop shortcut via SSM. |
| **Image folder is empty in Windows** | Check that files exist in `/home/ray/projects/heyex-test-images/` on your Ubuntu PC first. |
| **xfreerdp not found** | Run `sudo apt install -y freerdp2-x11` once on your Ubuntu PC. |

---

## 🔧 One-time setup (already done, just for reference)

If you're setting this up on a new machine:

```bash
# 1. Install xfreerdp
sudo apt install -y freerdp2-x11

# 2. Clone the repo
git clone git@github.com:your-org/appway-backend.git ~/projects/appway-backend

# 3. Configure the AppWay AWS profile (account 911167932273)
aws configure --profile milani
#   AWS Access Key ID: <Milani key>
#   AWS Secret Access Key: <Milani secret>
#   Default region: eu-west-1
#   Default output format: json

# 4. Put AppWay.pem in the right place
cp AppWay.pem ~/.ssh/AppWay.pem
chmod 400 ~/.ssh/AppWay.pem

# 5. Create the local images folder
mkdir -p ~/projects/heyex-test-images
```

Then just run `./scripts/rdp-heyex.sh` as above.

---

## ℹ️ Key facts

| | |
|---|---|
| EC2 name | `Heyex2-testing` |
| EC2 instance ID | `i-02a7dd1797d85a099` |
| EC2 public IP (fixed) | `54.154.242.69` |
| RDP user | `Administrator` |
| AWS region | `eu-west-1` |
| AWS account | `911167932273` (AppWay — use `--profile milani`) |
| Local images folder | `/home/ray/projects/heyex-test-images/` |
| Windows images folder | `\\tsclient\heyex-images\` (live, only while RDP is open) |

> **Full technical docs:** `docs/heyex2-ubuntu-dongle.md`
