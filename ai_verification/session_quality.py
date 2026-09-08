"""
session_quality.py
==================
Session Integrity — structural capture-quality gate.

Motivated by the defence-in-depth posture expected of remote onboarding
solutions; results are reportable under the ISO/IEC 30107-3 evaluation
framework, which governs how PAD performance is tested and reported
rather than prescribing these specific checks.

Checks performed in a single decoding pass:
  - Frame rate ≥ 24 fps   (below this rPPG signal quality degrades significantly)
  - Resolution ≥ 640×480  (VGA minimum for reliable face PAD)
  - Duration 5–60 s        (lower: insufficient rPPG samples; upper: replay-attack gate)
  - Duplicate frame fraction ≤ 10 %  (high duplicates → frozen/replayed media)
  - Mean luminance 30–220            (too dark or overexposed → unreliable capture)
  - Reported vs. actual frame count within 20 %  (container-level manipulation indicator)
"""

from __future__ import annotations

import os
import tempfile

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MIN_FPS: float = 24.0           # rPPG (POS) needs ≥ 24 fps for reliable BVP extraction
MIN_WIDTH: int = 640
MIN_HEIGHT: int = 480
MIN_DURATION_S: float = 5.0    # POS_WANG needs ≥ 150 frames (5 s × 30 fps)
MAX_DURATION_S: float = 60.0   # clips > 60 s are likely pre-recorded

MAX_DUPLICATE_FRACTION: float = 0.10   # > 10 % identical frames → replay / frozen
MIN_MEAN_LUMINANCE: float = 30.0       # 0–255 scale; below = too dark
MAX_MEAN_LUMINANCE: float = 220.0      # above = over-exposed

FRAME_COUNT_TOLERANCE: float = 0.20    # allow 20 % drift between reported and actual

# Near-duplicate threshold: mean absolute pixel diff (downscaled grey) below this
# value is treated as a duplicated / frozen frame.
DUPLICATE_DIFF_THRESHOLD: float = 0.5

# Downscale target for per-frame processing (keeps runtime fast).
PROC_W: int = 160
PROC_H: int = 120

# Sample luminance every N frames (full decode is already happening for duplicates).
LUMI_EVERY: int = 10


def check_session_quality(video_bytes: bytes) -> dict:
    """
    Inspect a video clip for session integrity signals.

    All checks run in a single linear pass through the decoded frames so
    the overhead is bounded by the video length, not multiplied by the
    number of checks.

    Returns:
        {
            "pass":                 bool,
            "fps":                  float,
            "width":                int,
            "height":               int,
            "duration_s":           float,
            "frame_count_reported": int,
            "frame_count_actual":   int,
            "duplicate_fraction":   float,
            "mean_luminance":       float,
            "flags":                list[str],
        }
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return {
                "pass": False, "fps": 0.0, "width": 0, "height": 0,
                "duration_s": 0.0, "frame_count_reported": 0,
                "frame_count_actual": 0, "duplicate_fraction": 0.0,
                "mean_luminance": 0.0, "flags": ["video_unreadable"],
            }

        fps             = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        width           = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height          = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count_rep = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        actual      = 0
        dup_count   = 0
        luminances: list[float] = []
        prev_gray: np.ndarray | None = None

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            actual += 1

            small = cv2.resize(frame, (PROC_W, PROC_H))
            gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)

            if prev_gray is not None:
                if float(np.mean(np.abs(gray - prev_gray))) < DUPLICATE_DIFF_THRESHOLD:
                    dup_count += 1

            if actual % LUMI_EVERY == 0:
                luminances.append(float(np.mean(gray)))

            prev_gray = gray

        cap.release()
    finally:
        os.unlink(tmp_path)

    duration_s = actual / fps if fps > 0 else 0.0
    dup_frac   = dup_count / actual if actual > 0 else 0.0
    mean_lum   = float(np.mean(luminances)) if luminances else 0.0

    flags: list[str] = []

    if fps < MIN_FPS:
        flags.append(f"low_fps:{fps:.1f}")
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        flags.append(f"low_resolution:{width}x{height}")
    if duration_s < MIN_DURATION_S:
        flags.append(f"too_short:{duration_s:.1f}s")
    if duration_s > MAX_DURATION_S:
        flags.append(f"too_long:{duration_s:.1f}s")
    if dup_frac > MAX_DUPLICATE_FRACTION:
        flags.append(f"high_duplicate_fraction:{dup_frac:.2f}")
    if mean_lum < MIN_MEAN_LUMINANCE:
        flags.append(f"underexposed:{mean_lum:.1f}")
    elif mean_lum > MAX_MEAN_LUMINANCE:
        flags.append(f"overexposed:{mean_lum:.1f}")
    if frame_count_rep > 0:
        discrepancy = abs(actual - frame_count_rep) / frame_count_rep
        if discrepancy > FRAME_COUNT_TOLERANCE:
            flags.append(
                f"frame_count_mismatch:reported={frame_count_rep},actual={actual}"
            )

    return {
        "pass":                 len(flags) == 0,
        "fps":                  round(fps, 2),
        "width":                width,
        "height":               height,
        "duration_s":           round(duration_s, 2),
        "frame_count_reported": frame_count_rep,
        "frame_count_actual":   actual,
        "duplicate_fraction":   round(dup_frac, 4),
        "mean_luminance":       round(mean_lum, 2),
        "flags":                flags,
    }
