"""
temporal_consistency.py
=======================
Temporal Consistency — inter-frame continuity analysis.

Results are reportable under the ISO/IEC 30107-3 evaluation framework;
the standard governs PAD testing and reporting rather than prescribing
these specific checks.

Analyses inter-frame pixel differences to detect:
  - Abrupt frame discontinuities (cuts, stitched loops, replay artefacts).
  - Unnaturally uniform or frozen frame sequences.

Operates on greyscale downscaled frames — no new dependencies beyond OpenCV.
"""

from __future__ import annotations

import os
import tempfile

import cv2
import numpy as np

# IFD above this factor of the clip mean is a discontinuity jump.
JUMP_FACTOR: float = 4.0

# Clips with mean IFD below this are suspiciously static/frozen.
MIN_MEAN_IFD: float = 1.0

# If more than this fraction of frames are jumps, the clip fails.
MAX_JUMP_FRACTION: float = 0.05

PROC_WIDTH:  int = 160
PROC_HEIGHT: int = 120


def check_temporal_consistency(video_bytes: bytes) -> dict:
    """
    Compute inter-frame difference (IFD) statistics for a video clip.

    Returns:
        {
            "pass":            bool,
            "mean_ifd":        float,
            "std_ifd":         float,
            "jump_count":      int,
            "frames_analysed": int,
            "flags":           list[str],
        }
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return {
                "pass": False, "mean_ifd": 0.0, "std_ifd": 0.0,
                "jump_count": 0, "frames_analysed": 0,
                "flags": ["video_unreadable"],
            }

        ifds: list[float] = []
        prev_gray: np.ndarray | None = None

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, (PROC_WIDTH, PROC_HEIGHT))
            gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if prev_gray is not None:
                ifds.append(float(np.mean(np.abs(gray - prev_gray))))
            prev_gray = gray

        cap.release()
    finally:
        os.unlink(tmp_path)

    if len(ifds) < 2:
        return {
            "pass": False, "mean_ifd": 0.0, "std_ifd": 0.0,
            "jump_count": 0, "frames_analysed": len(ifds),
            "flags": ["insufficient_frames"],
        }

    arr       = np.array(ifds)
    mean_ifd  = float(np.mean(arr))
    std_ifd   = float(np.std(arr))
    threshold = mean_ifd * JUMP_FACTOR
    jumps     = int(np.sum(arr > threshold))
    jump_frac = jumps / len(arr)

    flags: list[str] = []
    if mean_ifd < MIN_MEAN_IFD:
        flags.append("static_or_frozen_video")
    if jump_frac > MAX_JUMP_FRACTION:
        flags.append(f"frame_discontinuities:{jumps}")

    return {
        "pass":            len(flags) == 0,
        "mean_ifd":        round(mean_ifd, 4),
        "std_ifd":         round(std_ifd, 4),
        "jump_count":      jumps,
        "frames_analysed": len(ifds),
        "flags":           flags,
    }
