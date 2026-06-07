# How to modify the PDF layout safely

This document describes the **edit → preview → compare** workflow for
iterating on the MyopicCNV+ report layout without touching the live
production pipeline.

---

## Quick reference

| Want to change… | Edit here |
|---|---|
| Any coordinate, font size, or colour | `P0`, `P_GAL`, `P5` dicts in `generator.py` |
| New text field or new palette colour | Add constant near the dicts, call `_text()` / `_text_right()` / `_text_centered()` in the relevant `_draw_pageN_*` function |
| Table card shift (card-under-logo distance) | `_P5_SHIFT` in `generator.py` **and** `TABLE_SHIFT_UP_PT` in `templates.py` — **keep in sync** |
| Verdict card gradient or drop shadow | `_render_verdict_card_png()` in `generator.py` |
| Gallery bbox overlay or confidence chip | `_render_bbox_png()` in `generator.py` |
| Template chrome (logo, tabs, footer band) | Rerun `templates.py` after editing the `.ai` source (see §5) |

---

## 1  Where to look at the three pages visually

After running the preview command (see §3), the outputs land here:

```
pdf_sandbox/outputs/previews/
  verdict_page.png    ← page 1: job info, verdict card, input files, app description
  image_page.png      ← page 2: gallery (up to 2 positive OCT images)
  table_page.png      ← page 3: per-image results table
```

These PNGs are gitignored (inside `pdf_sandbox/`) — they live only on
your local disk and are wiped + regenerated on every run.

---

## 2  Choosing a data preset

`preview.py` imports one of two presets from `sample_data.py`:

| Preset | Purpose | When to use |
|---|---|---|
| `STATIC_JOB` (default) | Wireframe with obvious placeholder strings (`"XXXXXXXXXXXXXXXXXXX"`, `"0000-01-01 00:00:00 UTC"`, `"X.X.X"`) | Iterating on layout backbone — placeholder strings don't distract from spacing/typography decisions |
| `MOCK_JOB` | Realistic data mirroring the `test-20260426_174801` scenario and the designer's reference PDF | Final visual comparison against `pdf_sandbox/designer_source/Myopic2 copia.pdf` |

Switch presets by editing the import at the top of `preview.py`:

```python
# wireframe (default)
from .sample_data import STATIC_JOB as JOB

# realistic
from .sample_data import MOCK_JOB as JOB
```

To see the gallery page with real OCT images, drop PNGs into
`pdf_sandbox/sample_images/` whose filenames match the `filename`
field of each `PerImageResult` in the chosen preset. If a file is
absent the generator renders a grey placeholder box instead.

---

## 3  The edit → preview → compare loop

```bash
# 1) Edit the layout
code appway_backend/report/generator.py

# 2) Regenerate the preview (takes ~1 s, no AWS, no DICOM needed)
uv run python -m appway_backend.report.preview

# 3) Inspect the relevant PNG
open pdf_sandbox/outputs/previews/verdict_page.png   # macOS
xdg-open pdf_sandbox/outputs/previews/verdict_page.png  # Linux

# 4) Side-by-side with the designer's reference
open pdf_sandbox/designer_source/Myopic2\ copia.pdf

# 5) Repeat until satisfied
```

The preview command also writes `pdf_sandbox/outputs/preview.pdf` — a
multi-page PDF you can open directly in any viewer.

---

## 4  Coordinate system gotcha

**All coordinates in `P0`, `P_GAL`, and `P5` are top-down points**
(the convention used by the designer and by PyMuPDF). ReportLab uses a
bottom-up origin, so every coordinate goes through one of two helpers
before being passed to `drawString` / `drawImage`:

| Helper | When to use |
|---|---|
| `_y(y_topdown)` | Image / rect placement — returns the *bottom* edge of the object |
| `_yb(y_topdown, font_size)` | Text placement — converts the designer's "cap-height top" bbox y₀ to a ReportLab baseline |

`_yb` applies `_ASCENT_RATIO = 0.968` — Montserrat's cap-ascent-to-size
ratio measured empirically against the parsed `.ai` spans. **Do not
change this constant** unless the font family itself changes; it is what
makes the generator's text baselines land within < 0.05 pt of the
designer's original bboxes.

If you add a new text element and it appears a few points too high or
too low, the first thing to check is whether you used `_yb` (for text)
or `_y` (for images).

---

## 5  When to rebuild the template PNGs

The template PNGs (`appway_backend/report/assets/page_template_*.png`)
are pre-rendered from the designer's `.ai` file and **committed to the
repo** alongside the Python source. You only need to rebuild them if:

- The designer delivers a revised `.ai` file, **or**
- You need to change `TABLE_SHIFT_UP_PT` in `templates.py` (and you
  must also keep `_P5_SHIFT` in `generator.py` equal to it).

To rebuild:

```bash
uv run python -m appway_backend.report.templates
```

This overwrites `appway_backend/report/assets/page_template_{summary,gallery,table}.png`.
After rebuilding, commit the updated PNGs.

> ⚠️  Do **not** run `templates.py` as part of normal layout iteration —
> it is only needed when the chrome (logo, gradient frames, footer band)
> actually changes. Running it unnecessarily replaces committed assets
> with identical files and pollutes the git diff.

---

## 6  What NOT to touch when only tweaking the layout

| File | Why hands off |
|---|---|
| `appway_backend/pdf_report.py` | Public API shim — changing it shifts the interface that `epdf_generator.py` and tests depend on |
| `appway_backend/epdf_generator.py` | Production DICOM wrapper — it delegates to `pdf_report.build_pdf()` and should not contain any layout logic |
| `appway_backend/report/assets/` TTF files | Committed runtime assets; removing or renaming them breaks font registration on the production EC2 |
| `appway_backend/report/assets/` PNG files | Only replace after an intentional `templates.py` rebuild (see §5) |

All layout work lives exclusively under `appway_backend/report/`:
`generator.py` for the layout engine, `sample_data.py` for sandbox
presets, `preview.py` for the sandbox runner, `templates.py` for the
chrome rebuild step.

---

## 7  Troubleshooting: browser preview broken after `apt upgrade` on Ubuntu 24.04

### Symptom

Running the preview command works fine (`uv run python -m appway_backend.report.preview`),
but opening the PNG in any Puppeteer-controlled browser fails with:

```
No usable sandbox!  kernel.apparmor_restrict_unprivileged_userns = 1
```

### Root cause

Ubuntu 23.10+ and Ubuntu 24.04 LTS ship with an AppArmor policy that blocks
**unprivileged user-namespace creation**. Puppeteer's bundled Chromium uses a user
namespace for its renderer sandbox; when the restriction is enabled, Chrome aborts
at startup. The sysctl knob is:

```
kernel.apparmor_restrict_unprivileged_userns
```

A routine `apt upgrade` can re-assert this value to `1` if the kernel or the
AppArmor package is updated.

### Fix (one-time, persists across reboots)

```bash
# Disable immediately (takes effect at once, no reboot needed):
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

# Persist the setting so it survives reboots and future kernel upgrades:
echo "kernel.apparmor_restrict_unprivileged_userns=0" | \
  sudo tee /etc/sysctl.d/60-apparmor-userns.conf
```

**Verify** the knob is off:

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns
# Expected output: kernel.apparmor_restrict_unprivileged_userns = 0
```

### Why this is safe on the backend EC2

This machine is a single-user development / production server. The risk model
for `apparmor_restrict_unprivileged_userns` is multi-tenant shared hosting where
an unprivileged attacker could exploit namespace features. On a single-purpose EC2
with IAM-role access control, network isolation, and no interactive user accounts
beyond the ubuntu admin, the restriction adds no meaningful security benefit while
actively breaking every Chromium-based tool (Puppeteer, the VS Code browser preview,
etc.).

---

## 8  How preview and production share exactly the same code path

```
preview.py
  └─ calls generator.build_pdf(job, out_path)

production (appway-worker systemd service)
  └─ epdf_generator.generate_epdf_dcm()
       └─ pdf_report.build_pdf()         ← thin re-export shim
            └─ generator.build_pdf()     ← same function
```

There is no separate "preview mode" vs "production mode" inside the
generator. If `verdict_page.png` looks right after your edit, the
production PDF will look right too — no AWS credentials, no DICOM files,
no job queue needed to verify layout changes.

---

## 9  Dynamic fields: where the PDF's runtime values come from

| PDF field | Location on page | Source |
|---|---|---|
| **Job ID** | Verdict page — JOB INFORMATION | Passed in by the worker as `ReportJob.job_id` |
| **Processed** timestamp | Verdict page — JOB INFORMATION | UTC wall-clock time at the moment `generate_epdf_dcm()` is called |
| **Software: MyopicCNV+ v…** | Verdict page — JOB INFORMATION | `.env` → `CLINICAL_TRIAL_PROTOCOL_VERSION` (see below) |
| **Verdict card** (ACTIVE / INACTIVE + sub-line) | Verdict page — AI ANALYSIS RESULT | `inference_result["verdict"]` + per-image counts from inference |
| **Input file** filename + metadata | Verdict page — INPUT FILES RECEIVED | Read from the input `.dcm` file headers by `epdf_generator._collect_input_info()` |
| **Gallery images** | Image pages | Extracted B-scan PNGs written by `processor.py`; red bbox + confidence chip drawn by `_render_bbox_png()` |
| **Per-image table rows** | Table page | `inference_result["per_image"]` list |

### Software version: `.env` is the source of truth

The `Software: MyopicCNV+ v…` line on the verdict page is set by the
`CLINICAL_TRIAL_PROTOCOL_VERSION` environment variable, which lives in
`.env`:

```
# .env
CLINICAL_TRIAL_PROTOCOL_VERSION=1.0.0
```

**How it works:**  `build_pdf()` in `generator.py` reads this variable
(via `appway_backend.config`, which calls `load_dotenv()`) at the
moment each PDF is generated — **not** at import time.  Any value in
`ReportJob.software_version` passed by the caller is silently replaced
with the env-var value, so the data-flow is always:

```
.env  →  config.CLINICAL_TRIAL_PROTOCOL_VERSION
      →  build_pdf() override
      →  "Software: MyopicCNV+ v1.0.0" on verdict page
```

The same variable also appears in the error-PDF body text
(`epdf_generator._build_error_pdf_bytes`) and in the DICOM tag
`ClinicalTrialProtocolID (0012,0020)` — making `.env` the single,
authoritative place to bump the protocol version.

**To change the displayed version:**

```bash
# 1. Edit .env
#    CLINICAL_TRIAL_PROTOCOL_VERSION=1.1.0

# 2. Restart the worker to pick up the new value
sudo systemctl restart appway-worker

# 3. Verify in the sandbox (no restart needed for preview runs,
#    because uv starts a fresh process each time)
uv run python -m appway_backend.report.preview
# → verdict_page.png should now show "Software: MyopicCNV+ v1.1.0"
```

`pyproject.toml`'s `version` field and the DICOM `SoftwareVersions`
tag `(0018,1020)` are **not** affected — those remain the Python package
version and are unrelated to the clinical-trial protocol version.
