"""
YOLO inference for MyopicCNV+.

Self-contained adaptation of the original Streamlit webapp inference code
(see /home/ubuntu/mcnv/src/web_pages/inference_yolo.py) with all Streamlit
dependencies removed.

Usage:

    from appway_backend.inference import run_inference
    result = run_inference([Path("img001.png"), Path("img002.png"), ...])
    # result = {
    #   "verdict":         "Positive" | "Negative",
    #   "processing_time": 3.17,
    #   "per_image": [
    #       {"filename": "img001.png", "pred": 1, "label": "Positive",
    #        "conf": 0.724, "bbox": [x1, y1, x2, y2]},
    #       ...
    #   ],
    # }

Model loading is lazy and thread-safe: the first call downloads the .pt file
from S3 to MODEL_LOCAL_PATH (if not already on disk), loads it into memory,
then every subsequent call reuses the in-memory model.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from time import time
from typing import List, Optional

import boto3

from . import config

logger = logging.getLogger(__name__)


# ───────────────────────────── constants ─────────────────────────────
CLASS_NAMES = {0: "Negative", 1: "Positive"}


# ───────────────────────────── model singleton ───────────────────────
_model = None                # lazily-loaded ultralytics.YOLO instance
_model_lock = threading.Lock()
_device: Optional[str] = None


def _pick_device() -> str:
    """Return 'cuda:0' if a GPU is available, else 'cpu'."""
    try:
        import torch  # imported lazily so a no-GPU container can still import this module
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _download_model_from_s3(bucket: str, key: str, local_path: Path) -> None:
    """Download the YOLO .pt weights from S3 to local_path."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading YOLO model s3://%s/%s → %s", bucket, key, local_path)
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    s3.download_file(bucket, key, str(local_path))
    logger.info("YOLO model downloaded (%d bytes)", local_path.stat().st_size)


def load_model():
    """
    Load (or reuse) the singleton YOLO model.

    First call: downloads from S3 if the local file is missing, then
    instantiates ultralytics.YOLO(MODEL_LOCAL_PATH).
    Subsequent calls: return the cached instance.
    """
    global _model, _device
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        local_path = config.MODEL_LOCAL_PATH
        if not local_path.exists():
            _download_model_from_s3(
                bucket=config.MODEL_S3_BUCKET,
                key=config.MODEL_S3_KEY,
                local_path=local_path,
            )
        else:
            logger.info("Using cached YOLO model at %s", local_path)

        # Import ultralytics lazily: avoids heavy torch import at module load
        from ultralytics import YOLO  # noqa: WPS433

        _device = _pick_device()
        logger.info("Loading YOLO model on device=%s …", _device)
        _model = YOLO(str(local_path))
        logger.info("YOLO model ready.")
        return _model


# ───────────────────────────── helpers ───────────────────────────────
def _format_output_single_element(cls, conf, data):
    """
    When YOLO returns several detections for a single image, pick the one with
    the highest confidence. Adapted from mcnv/src/web_pages/helpers.py.
    Returns (cls_max, conf_max, num_elements, data_max) all as 1-row tensors.
    """
    import torch  # local import
    max_idx = torch.argmax(conf)
    cls_max = cls[max_idx].unsqueeze(0)
    conf_max = conf[max_idx].unsqueeze(0)
    data_max = data[max_idx].unsqueeze(0)
    return cls_max, conf_max, conf.numel(), data_max


def _majority_vote_with_equality_check(predictions: List[int]) -> str:
    """
    Patient-level verdict: Positive if AT LEAST ONE image is Positive,
    otherwise Negative. Matches the original webapp semantics.
    """
    if any(p == 1 for p in predictions):
        return CLASS_NAMES[1]
    return CLASS_NAMES[0]


# ───────────────────────────── public API ────────────────────────────
def run_inference(png_paths: List[Path]) -> dict:
    """
    Run YOLO inference on a list of image paths and return a structured result.

    Args:
        png_paths: list of image file paths (PNG/JPEG both accepted by YOLO).

    Returns:
        dict with keys:
            verdict:         "Positive" | "Negative"  (patient-level)
            processing_time: float (seconds)
            per_image:       list of per-image dicts, see module docstring
    """
    if not png_paths:
        logger.warning("run_inference called with zero images — returning Negative.")
        return {
            "verdict": CLASS_NAMES[0],
            "processing_time": 0.0,
            "per_image": [],
        }

    model = load_model()
    device = _device or _pick_device()

    pred_list: List[int] = []
    per_image: List[dict] = []
    start = time()

    for img_path in sorted(png_paths):
        img_path = Path(img_path)
        try:
            result = model.predict(
                str(img_path),
                imgsz=config.YOLO_IMGSZ,
                conf=config.YOLO_CONF_THRESHOLD,
                iou=config.YOLO_IOU_THRESHOLD,
                device=device,
                augment=False,
                verbose=False,
            )[0].boxes

            if result.cls.numel() == 0:
                # No detection at all → Negative, no confidence, zero bbox
                pred = 0
                conf_val: Optional[float] = None
                bbox = [0.0, 0.0, 0.0, 0.0]
            else:
                cls_max, conf_max, _n, data_max = _format_output_single_element(
                    result.cls, result.conf, result.data,
                )
                pred = int(cls_max.item())
                conf_val = float(conf_max.item())
                # data row is [x1, y1, x2, y2, conf, cls]; take the first 4
                bbox = data_max[:, :4].cpu().numpy().tolist()[0]
        except Exception as e:
            logger.exception("Inference failed for %s — marking Negative.", img_path.name)
            pred = 0
            conf_val = None
            bbox = [0.0, 0.0, 0.0, 0.0]

        pred_list.append(pred)
        per_image.append({
            "filename": img_path.name,
            "pred":     pred,
            "label":    CLASS_NAMES[pred],
            "conf":     conf_val,
            "bbox":     bbox,
        })

    verdict = _majority_vote_with_equality_check(pred_list)
    processing_time = time() - start

    logger.info(
        "Inference complete: verdict=%s, %d images, %.2fs",
        verdict, len(per_image), processing_time,
    )

    return {
        "verdict":         verdict,
        "processing_time": processing_time,
        "per_image":       per_image,
    }
