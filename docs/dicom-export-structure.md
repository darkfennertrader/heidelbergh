# HEYEX DICOM Export Structure — Reference

This document explains the **IHE-PDI / DICOMDIR media folder** that HEYEX 2
produces when you export a study. It also clarifies the difference between
the *input* DICOM (what HEYEX sends to AppWay) and the *output* DICOM
(the EncapsulatedPDF result AppWay sends back).

---

## Background — what `docs/examples/DICOM-Export-Test/` is

`DICOM-Export-Test/` was created by:
1. Sending a test OPT study into HEYEX 2 via the AppWay interface.
2. Our backend processed it → generated the ePDF report → wrote it back to HEYEX.
3. **Then** the full study was exported from HEYEX as a portable DICOM media folder.

So this folder is a **snapshot of a successful round-trip**, containing:
- `00000000` — the OPT input after HEYEX re-serialised it
- `00000001` — **our ePDF result**, as HEYEX stored it internally and then exported

It is **not** the format of what HEYEX sends to AppWay as input — that is a
flat `.dcm` file (see `docs/examples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm`
for the Heidelberg ground-truth input example).

---

## Folder layout

```
DICOM-Export-Test/
├── DICOMDIR                         ← DICOM directory index (2.5 KB)
│                                      SOP: Media Storage Directory Storage
│                                      (1.2.840.10008.1.3.10)
├── DICOM/
│   └── MC1/2/2/
│       ├── 1/
│       │   └── 00000000  (3.2 MB)   ← OPT input (after HEYEX re-serialisation)
│       └── 3/
│           └── 00000001  (5.0 MB)   ← Encapsulated PDF (our ePDF result)
└── IHE_PDI/
    ├── DICOMDIR.HTM                  ← Human-readable HTML index (ignore)
    └── images/                       ← Icons for the HTML viewer (ignore)
```

---

## DICOMDIR — the study tree

The DICOMDIR encodes the full **PATIENT → STUDY → SERIES → IMAGE** hierarchy:

```
[PATIENT]  PatientID=bbff7a25-d32c-4192-9330-0bb01d49f746
           PatientName=E0ee1bc04-dd6f^Qf73528ea  (de-identified)
  [STUDY]  StudyInstanceUID=1.2.826.0.1.3680043.8.498.885850...
           Date=20150624
           Desc="External Patient: External Patient: HRA + OCT"
    [SERIES] Mod=OPT  SeriesNumber=2
             Desc="APPWAY TEST (10fr from images)"
      [IMAGE] → DICOM/MC1/2/2/1/00000000
    [SERIES] Mod=DOC  SeriesNumber=1000
             Desc="MyopicCNV+ Result for Job final-a4e258b8-..."
      [ENCAP DOC] → DICOM/MC1/2/2/3/00000001
```

Key relationships:
- Both series share the **same StudyInstanceUID** — HEYEX links the result
  back to the patient's existing study. This is how it appears alongside
  the original OCT scan on the ophthalmologist's screen.
- **SeriesNumber = 1000** is the AppWay convention for result series (`DOC`).

---

## File 1 — OPT input (`DICOM/MC1/2/2/1/00000000`)

The OCT B-scan stack, produced by Heidelberg Engineering and (in this test
case) re-serialised by HEYEX on export.

| Tag | Value |
|-----|-------|
| **SOP Class** | Ophthalmic Tomography Image Storage (`1.2.840.10008.5.1.4.1.1.77.1.5.4`) |
| **Transfer Syntax** | JPEG Lossless, Non-Hierarchical (`1.2.840.10008.1.2.4.70`) |
| **Modality** | `OPT` |
| **Manufacturer** | `Heidelberg Engineering` |
| **Frames** | 10 |
| **Rows × Columns** | 596 × 1008 |
| **BitsAllocated / Stored** | 8 / 8 |
| **PhotometricInterpretation** | `MONOCHROME2` |
| **SamplesPerPixel** | 1 |
| **StudyDate** | `20150624` |
| **StudyDescription** | `External Patient: External Patient: HRA + OCT` |
| **SeriesDescription** | `APPWAY TEST (10fr from images)` |
| **SeriesNumber** | `2` |
| **PatientID** | `bbff7a25-d32c-4192-9330-0bb01d49f746` |

> **Note:** The Heidelberg ground-truth input (`20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm`)
> also uses Ophthalmic Tomography Image Storage and JPEG-Lossless, confirming
> our JPEG-Lossless decoder path (pylibjpeg) is required on the worker.

---

## File 2 — Encapsulated PDF result (`DICOM/MC1/2/2/3/00000001`)

Our ePDF report wrapped as a DICOM Encapsulated PDF. This is what
`appway_backend/epdf_generator.py` produces (before HEYEX re-packages it
on export — HEYEX does not materially change the payload).

| Tag | Value |
|-----|-------|
| **SOP Class** | Encapsulated PDF Storage (`1.2.840.10008.5.1.4.1.1.104.1`) |
| **Transfer Syntax** | Explicit VR Little Endian (`1.2.840.10008.1.2.1`) |
| **Modality** | `DOC` |
| **Manufacturer** | `MyopicCNV+` |
| **DocumentTitle** | `MyopicCNV+ Result for Job <job-id>` |
| **MIMETypeOfEncapsulatedDocument** | `application/pdf` |
| **EncapsulatedDocument** | raw PDF bytes (starts `%PDF-`, ends `%%EOF`) |
| **StudyInstanceUID** | **same** as the OPT input → linked into the same study |
| **SeriesNumber** | **1000** (AppWay convention) |
| **PatientID / PatientName** | copied verbatim from the input OPT |
| **StudyDate / StudyDescription** | copied verbatim from the input OPT |

---

## What to watch for when HEYEX sends a file to AppWay

HEYEX sends **a single flat `.dcm` file** (not the full IHE-PDI folder) to
the AppWay backend via S3. It looks like:

```
20260512072803565_7e2732bcb5674d5f815da5731a9a70ad.dcm
```

Filename format: `<YYYYMMDDHHMMSSMMM>_<hex-hash>.dcm`

That file is the same structure as `00000000` above — Ophthalmic Tomography
Image Storage, JPEG-Lossless, multi-frame. Our worker:

1. Downloads it from S3.
2. Decodes frames via `pydicom` + `pylibjpeg` (required for JPEG-Lossless).
3. Runs inference per-frame.
4. Builds the PDF report via `appway_backend.report.generator.build_pdf()`.
5. Wraps the PDF as an Encapsulated PDF DICOM via `appway_backend.epdf_generator`.
6. Uploads the result DICOM to S3 → SNS → HEYEX imports it.

---

## Key DICOM UIDs reference

| Identifier | UID |
|------------|-----|
| OPT SOP Class | `1.2.840.10008.5.1.4.1.1.77.1.5.4` |
| Encapsulated PDF SOP Class | `1.2.840.10008.5.1.4.1.1.104.1` |
| JPEG Lossless transfer syntax | `1.2.840.10008.1.2.4.70` |
| Explicit VR Little Endian | `1.2.840.10008.1.2.1` |
| Media Storage Directory (DICOMDIR) | `1.2.840.10008.1.3.10` |

---

## Related files

| File | Purpose |
|------|---------|
| `docs/examples/DICOM-Export-Test/` | Reference round-trip export (this document) |
| `docs/examples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm` | Heidelberg ground-truth OPT input sample |
| `docs/examples/test_443816.dcm` | Home-built test input (10 JPEGs wrapped into OPT DICOM) |
| `docs/examples/Example HD AppWay Result Report DICOM ePDF.dcm` | Heidelberg-provided ePDF reference output |
| `appway_backend/epdf_generator.py` | Our Encapsulated PDF DICOM writer |
| `appway_backend/processor.py` | Main processing pipeline (decode → infer → build PDF → wrap) |
