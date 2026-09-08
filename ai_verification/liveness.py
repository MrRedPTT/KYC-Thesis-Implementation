"""
liveness.py
===========
Passive Presentation Attack Detection (PAD) via Remote Photoplethysmography (rPPG).

Method: POS (Plane-Orthogonal-to-Similarity) — Wang et al. (2017).

Rationale for choosing rPPG over MiniFASNet (texture-CNN):
  In the thesis experimental evaluation (Chapter ExperimentalResults), MiniFASNet
  failed to detect Hedra / Google Veo 3 deepfake videos entirely (0 spoof frames
  on both test sequences — a textbook Generalisation Failure against LDM-based
  synthetic media). rPPG-POS correctly classified both videos as SPOOF (peakiness
  1.23 and 1.59, both below the 1.70 liveness threshold), making it the only tested
  mechanism resistant to current diffusion-based generative models.

Input:  A short video clip (MP4, WebM, AVI) of at least ~4 s at 30 fps containing
        a frontal face. rPPG cannot operate on a single still image.

Dependencies:
  - opencv-python  (already in requirements.txt)
  - numpy          (transitive)
  - scipy          (transitive via rPPG-Toolbox)
  - rPPG-Toolbox   (cloned at Tools/rPPG-Toolbox)
"""

from __future__ import annotations

import os
import sys
import tempfile

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# rPPG-Toolbox path injection
#
# rPPG-Toolbox is not on PyPI, so it is cloned rather than pip-installed and
# has to be put on sys.path by hand. Candidates are tried in order and all are
# resolved relative to this file, so they hold regardless of cwd:
#
#   1. $RPPG_TOOLBOX_PATH        explicit override, wins if set
#   2. <repo>/Tools/rPPG-Toolbox the documented location (see README)
#   3. <repo>/../Tools/...       sibling of the repository
#   4. <repo>/../../Tools/...    legacy layout, when the code lived inside the
#                                thesis directory as implementation/
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, ".."))

_RPPG_CANDIDATES = [
    os.environ.get("RPPG_TOOLBOX_PATH"),
    os.path.join(_REPO, "Tools", "rPPG-Toolbox"),
    os.path.normpath(os.path.join(_REPO, "..", "Tools", "rPPG-Toolbox")),
    os.path.normpath(os.path.join(_REPO, "..", "..", "Tools", "rPPG-Toolbox")),
]

_RPPG_TOOLBOX = next(
    (os.path.normpath(p) for p in _RPPG_CANDIDATES if p and os.path.isdir(p)),
    None,
)

if _RPPG_TOOLBOX and _RPPG_TOOLBOX not in sys.path:
    sys.path.insert(0, _RPPG_TOOLBOX)

try:
    from unsupervised_methods.methods.POS_WANG import POS_WANG
    from evaluation.post_process import _calculate_fft_hr
    _RPPG_AVAILABLE = True
except ImportError:
    _RPPG_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants (tuned on the thesis experimental dataset)
# ---------------------------------------------------------------------------

# Peakiness threshold — values above this indicate a genuine cardiac pulse.
# Basis: genuine recordings produce peakiness >> 1.70;
# Hedra-generated deepfakes produced 1.23 and 1.59 in thesis experiments.
LIVENESS_THRESHOLD: float = 1.70

# Minimum successfully-extracted face crops to produce a valid decision.
MIN_FRAMES: int = 30

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_face_cascade: cv2.CascadeClassifier | None = None


def _get_cascade() -> cv2.CascadeClassifier:
    global _face_cascade
    if _face_cascade is None:
        xml = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(xml)
    return _face_cascade


def _extract_face_crops(video_bytes: bytes) -> tuple[list[np.ndarray], float]:
    """
    Decode video bytes, detect the largest frontal face in each frame, and
    return normalised RGB crops suitable for POS_WANG.

    Returns:
        (crops, fps)
            crops — list of (128, 128, 3) float32 arrays in [0, 1]
            fps   — frames-per-second of the source video
    """
    cascade = _get_cascade()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            raise ValueError(
                "Could not decode video — unsupported format or corrupt file."
            )

        fps: float = cap.get(cv2.CAP_PROP_FPS) or 30.0
        crops: list[np.ndarray] = []

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
            )
            if len(faces) == 0:
                continue

            # Use the largest detected face
            x, y, w, h = sorted(faces, key=lambda r: r[2] * r[3], reverse=True)[0]
            crop = frame[y : y + h, x : x + w]
            crop = cv2.resize(crop, (128, 128))
            crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            crops.append(crop_rgb)

        cap.release()
    finally:
        os.unlink(tmp_path)

    return crops, fps


def _compute_peakiness(bvp: np.ndarray, fs: float) -> tuple[float, float]:
    """
    Derive heart rate and peakiness from a BVP signal via FFT.

    Peakiness = peak_power / mean_power within the 45–150 BPM band.
    A live cardiac signal produces a sharp spectral peak (peakiness > LIVENESS_THRESHOLD).
    Synthetic / replayed media lacks this periodic oscillation.

    Returns:
        (hr_bpm, peakiness)
    """
    if len(bvp) < 64:
        return 0.0, 0.0

    hr = float(_calculate_fft_hr(bvp, fs=fs))

    fft_mag = np.abs(np.fft.rfft(bvp))
    freqs = np.fft.rfftfreq(len(bvp), 1.0 / fs)

    mask = (freqs >= 0.75) & (freqs <= 2.5)  # 45–150 BPM
    if not np.any(mask):
        return hr, 0.0

    hr_band = fft_mag[mask]
    peakiness = float(np.max(hr_band)) / float(np.mean(hr_band)) if np.mean(hr_band) > 0 else 0.0

    return hr, peakiness


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_liveness(video_bytes: bytes) -> dict:
    """
    Run rPPG-POS liveness detection on a short video clip.

    Args:
        video_bytes: Raw bytes of a video file (MP4, WebM, or AVI).
                     A minimum of ~4 s at 30 fps is recommended for a reliable
                     result. The clip must contain a visible frontal face.

    Returns:
        {
            "is_live":  bool,   # True if peakiness exceeds LIVENESS_THRESHOLD
            "score":    float,  # Peakiness value (higher → more likely live)
            "hr_bpm":   float,  # Estimated heart rate in BPM
            "method":   str,    # "rPPG-POS"
            "detail":   str,    # Human-readable explanation of the verdict
        }

    Raises:
        RuntimeError: If rPPG-Toolbox dependencies are not importable.
        ValueError:   If the video cannot be decoded or no face is detected.
    """
    if not _RPPG_AVAILABLE:
        if _RPPG_TOOLBOX is None:
            raise RuntimeError(
                "rPPG-Toolbox was not found. Clone it into "
                f"'{os.path.join(_REPO, 'Tools', 'rPPG-Toolbox')}', or set "
                "RPPG_TOOLBOX_PATH to wherever it lives. See the README."
            )
        raise RuntimeError(
            f"rPPG-Toolbox was found at '{_RPPG_TOOLBOX}' but is not importable. "
            "Install its dependencies (pip install -r requirements.txt inside it)."
        )

    crops, fps = _extract_face_crops(video_bytes)

    if len(crops) < MIN_FRAMES:
        return {
            "is_live": False,
            "score":   0.0,
            "hr_bpm":  0.0,
            "method":  "rPPG-POS",
            "detail": (
                f"Insufficient face frames: {len(crops)} extracted, {MIN_FRAMES} required. "
                f"Ensure the clip is at least {MIN_FRAMES / fps:.1f} s long "
                f"with a visible frontal face."
            ),
        }

    frames_np = np.array(crops)        # (N, 128, 128, 3) float32
    bvp = POS_WANG(frames_np, fps)     # BVP signal (N,)
    hr, peakiness = _compute_peakiness(bvp, fps)

    is_live = peakiness > LIVENESS_THRESHOLD
    return {
        "is_live": is_live,
        "score":   round(peakiness, 4),
        "hr_bpm":  round(hr, 2),
        "method":  "rPPG-POS",
        "detail": (
            f"Peakiness {peakiness:.4f} "
            f"({'above' if is_live else 'below'} threshold {LIVENESS_THRESHOLD}). "
            f"Estimated HR: {hr:.1f} BPM."
        ),
    }
