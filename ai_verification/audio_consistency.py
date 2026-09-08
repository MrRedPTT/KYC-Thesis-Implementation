"""
audio_consistency.py
====================
Audio Consistency — voice-track authenticity analysis.

Addresses manipulated audio evidence in remote onboarding recordings,
within the category of risks the EBA remote onboarding guidance expects
institutions to guard against.

Analyses the audio track extracted from the submitted video to detect:
  1. Audio track presence     — video must carry an audio stream
  2. Speech activity ratio    — silence fraction must not exceed 70 %
                                (a pre-recorded silent clip has no speech)
  3. Spectral flatness        — Wiener entropy; TTS/voice-conversion output
                                has a measurably flatter spectrum than
                                natural speech (harmonic peaks are smoothed)
  4. Short-term energy variance — real speech has natural amplitude
                                  fluctuation; unnaturally uniform energy
                                  signals a synthetic or looped audio source
  5. Lip-audio synchronisation — Pearson correlation between the per-frame
                                 audio RMS envelope and the pixel-motion in
                                 the estimated mouth region; poor correlation
                                 is a deepfake / dubbing indicator

Audio extraction relies on the system ffmpeg binary (already on PATH after
the rPPG-Toolbox installation). Signal analysis uses only scipy + numpy
(already in requirements).
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile

import cv2
import numpy as np
from scipy import signal as sp_signal

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16_000  # Hz — sufficient for speech analysis

# Silence gate: frames whose RMS falls below this are counted as silent.
SILENCE_RMS_THRESH: float = 0.01
MAX_SILENCE_RATIO:  float = 0.70     # > 70 % silent → flag

# Energy variance: variance of per-frame RMS across the clip.
# Unnaturally low variance = looped / synthetic audio.
MIN_ENERGY_VARIANCE: float = 3e-5

# Spectral flatness (Wiener entropy): geometric-mean / arithmetic-mean, computed
# here over the MAGNITUDE spectrum (the canonical definition uses the power
# spectrum, so these values are not comparable to published power-spectrum
# figures). Range 0–1; low = harmonic/tonal, high = noise-like. Vocoder synthesis
# tends to over-smooth the spectral envelope, raising flatness. This threshold is
# calibrated against the prototype's reference clips, not taken from literature —
# recalibrate once a labelled genuine/synthetic corpus is available.
MAX_SPECTRAL_FLATNESS: float = 0.45

# Lip-audio synchronisation: Pearson r between audio RMS envelope and mouth-
# region pixel variance.  A strongly *negative* correlation means audio and
# lip motion are anti-correlated — a signature of dubbed or replaced audio.
# Near-zero is normal when the subject is not actively speaking.
# We only flag values below this (i.e. strong anti-correlation).
MIN_LIP_SYNC_CORR: float = -0.30

# Frame size for short-time audio analysis (power of two for FFT efficiency).
STFT_FRAME: int = 512
STFT_HOP:   int = 128

# OpenCV Haar cascade for face detection (bundled with opencv).
_CASCADE_PATH: str = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_audio(video_path: str) -> np.ndarray | None:
    """
    Use ffmpeg to decode the audio track to mono float32 PCM.
    Returns a 1-D float32 array, or None when no audio stream is present.
    """
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",                          # strip video
        "-ac", "1",                     # mono
        "-ar", str(SAMPLE_RATE),
        "-f", "f32le",                  # raw 32-bit float little-endian
        "-",                            # stdout
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    raw = proc.stdout
    if not raw:
        return None

    n_samples = len(raw) // 4
    audio = np.array(struct.unpack(f"<{n_samples}f", raw), dtype=np.float32)
    # Clip to [-1, 1] in case of clipping artefacts
    return np.clip(audio, -1.0, 1.0)


def _frame_rms(audio: np.ndarray, hop: int = STFT_HOP) -> np.ndarray:
    """Per-hop RMS energy envelope."""
    n_hops = max(1, len(audio) // hop)
    rms = np.zeros(n_hops, dtype=np.float32)
    for i in range(n_hops):
        chunk = audio[i * hop: (i + 1) * hop]
        rms[i] = float(np.sqrt(np.mean(chunk ** 2)))
    return rms


def _spectral_flatness(audio: np.ndarray) -> float:
    """
    Compute mean Wiener entropy across STFT frames.
    Geometric mean / arithmetic mean of the magnitude spectrum per frame,
    averaged over all frames with non-trivial energy.
    """
    _, _, Zxx = sp_signal.stft(
        audio, fs=SAMPLE_RATE, nperseg=STFT_FRAME, noverlap=STFT_FRAME - STFT_HOP
    )
    mag = np.abs(Zxx).T  # shape (n_frames, n_freqs)

    flatness_vals: list[float] = []
    eps = 1e-10
    for frame in mag:
        frame = frame + eps
        if np.sum(frame) < eps * len(frame) * 10:
            continue  # near-silence frame — skip
        log_mean = float(np.mean(np.log(frame)))
        mean_val = float(np.mean(frame))
        flatness_vals.append(np.exp(log_mean) / mean_val)

    return float(np.mean(flatness_vals)) if flatness_vals else 0.0


def _mouth_motion_signal(video_path: str, n_samples: int) -> np.ndarray | None:
    """
    Estimate per-frame mouth-region motion as the standard deviation of
    pixel values in the lower third of the face bounding box.

    Returns a float array of length n_samples (resampled to match the audio
    RMS envelope), or None if no face is detected in enough frames.
    """
    cascade = cv2.CascadeClassifier(_CASCADE_PATH)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    motion: list[float] = []
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                         minSize=(60, 60))
        if len(faces) == 0:
            motion.append(0.0)
            continue

        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        # Mouth region ≈ lower 35 % of face bounding box
        my = y + int(h * 0.65)
        mouth_roi = gray[my: y + h, x: x + w]
        if mouth_roi.size == 0:
            motion.append(0.0)
        else:
            motion.append(float(np.std(mouth_roi.astype(np.float32))))

    cap.release()

    if len(motion) < 10:
        return None

    # Resample to n_samples so the two signals align in time
    motion_arr = np.array(motion, dtype=np.float32)
    if len(motion_arr) == n_samples:
        return motion_arr
    indices = np.linspace(0, len(motion_arr) - 1, n_samples)
    return np.interp(indices, np.arange(len(motion_arr)), motion_arr).astype(np.float32)


def _pearson_r(a: np.ndarray, b: np.ndarray) -> float:
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_audio_consistency(video_bytes: bytes) -> dict:
    """
    Inspect the audio track of a video clip for audio-consistency signals.

    Returns:
        {
            "pass":                bool,
            "has_audio":           bool,
            "silence_ratio":       float,
            "spectral_flatness":   float,
            "energy_variance":     float,
            "lip_sync_corr":       float | None,
            "flags":               list[str],
        }
    """
    # Write video to a temp file so ffmpeg and cv2 can both open it.
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        audio = _extract_audio(tmp_path)

        if audio is None or len(audio) == 0:
            return {
                "pass": False,
                "has_audio": False,
                "silence_ratio": 1.0,
                "spectral_flatness": 0.0,
                "energy_variance": 0.0,
                "lip_sync_corr": None,
                "flags": ["no_audio_track"],
            }

        # ── Check 2: silence ratio ────────────────────────────────────────
        rms_env = _frame_rms(audio)
        silence_ratio = float(np.mean(rms_env < SILENCE_RMS_THRESH))

        # ── Check 3: spectral flatness ────────────────────────────────────
        flatness = _spectral_flatness(audio)

        # ── Check 4: energy variance ──────────────────────────────────────
        energy_var = float(np.var(rms_env))

        # ── Check 5: lip-audio synchronisation ───────────────────────────
        lip_sync_corr: float | None = None
        if silence_ratio < MAX_SILENCE_RATIO:   # only meaningful when speech present
            mouth = _mouth_motion_signal(tmp_path, len(rms_env))
            if mouth is not None:
                lip_sync_corr = round(_pearson_r(rms_env, mouth), 4)

    finally:
        os.unlink(tmp_path)

    flags: list[str] = []

    if silence_ratio > MAX_SILENCE_RATIO:
        flags.append(f"high_silence_ratio:{silence_ratio:.2f}")
    if flatness > MAX_SPECTRAL_FLATNESS:
        flags.append(f"flat_spectrum:{flatness:.3f}")
    if energy_var < MIN_ENERGY_VARIANCE:
        flags.append(f"low_energy_variance:{energy_var:.2e}")
    if lip_sync_corr is not None and lip_sync_corr < MIN_LIP_SYNC_CORR:
        flags.append(f"poor_lip_sync:{lip_sync_corr:.3f}")

    return {
        "pass":              len(flags) == 0,
        "has_audio":         True,
        "silence_ratio":     round(silence_ratio, 4),
        "spectral_flatness": round(flatness, 4),
        "energy_variance":   round(energy_var, 6),
        "lip_sync_corr":     lip_sync_corr,
        "flags":             flags,
    }
