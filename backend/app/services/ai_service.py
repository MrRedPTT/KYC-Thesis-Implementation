"""
ai_service.py
=============
Bridge between the FastAPI backend and the AI verification modules.
Each function delegates to the corresponding module in implementation/ai_verification/.
"""

import sys
import tempfile
import os
from pathlib import Path

import cv2

# Allow importing the sibling ai_verification package
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ai_verification.document_auth        import authenticate_document
from ai_verification.face_verification    import verify_face
from ai_verification.liveness             import check_liveness
from ai_verification.session_quality      import check_session_quality
from ai_verification.temporal_consistency import check_temporal_consistency
from ai_verification.audio_consistency    import check_audio_consistency
from ai_verification.risk_scoring         import compute_risk_score as _compute_risk


def _extract_middle_frame(video_bytes: bytes) -> bytes:
    """
    Write video bytes to a temp file, seek to the middle frame, and return
    that frame as JPEG bytes. Used to extract a still selfie from the
    biometric video clip for ArcFace 1:1 matching.
    """
    suffix = ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(video_bytes)
        tmp_path = f.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        target = max(0, total // 2)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return b""
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return bytes(buf)
    finally:
        os.unlink(tmp_path)


async def verify_document(file_bytes: bytes, content_type: str) -> dict:
    """
    Run OCR + structural authentication on an uploaded identity document.

    Returns:
        dict with keys: ocr_data, authentic, confidence, flags
    """
    return await authenticate_document(file_bytes, content_type)


async def verify_biometrics(file_bytes: bytes, reference_image: bytes) -> dict:
    """
    Run face verification (1:1 match) and rPPG-POS liveness detection.

    Args:
        file_bytes:      Raw bytes of a short video clip (MP4, WebM, AVI).
                         Minimum ~4 s at 30 fps with a visible frontal face.
                         rPPG liveness cannot operate on a single still image.
        reference_image: Portrait extracted from the identity document.

    Returns:
        dict with keys: face_match, match_score, liveness_pass, liveness_score,
                        liveness_hr_bpm
    """
    # ArcFace needs a still image; extract the middle frame from the video clip.
    live_frame = _extract_middle_frame(file_bytes)

    face_result     = await verify_face(live_frame, reference_image)
    liveness_result = await check_liveness(file_bytes)

    sq  = check_session_quality(file_bytes)
    tc  = check_temporal_consistency(file_bytes)
    ac  = check_audio_consistency(file_bytes)

    return {
        "face_match":                  face_result["match"],
        "match_score":                 face_result["score"],
        "liveness_pass":               liveness_result["is_live"],
        "liveness_score":              liveness_result["score"],
        "liveness_hr_bpm":             liveness_result.get("hr_bpm", 0.0),
        "session_quality_pass":        sq["pass"],
        "session_quality_flags":       sq["flags"],
        "temporal_consistency_pass":   tc["pass"],
        "temporal_consistency_flags":  tc["flags"],
        "audio_consistency_pass":      ac["pass"],
        "audio_consistency_flags":     ac["flags"],
        "audio_has_audio":             ac["has_audio"],
        "audio_silence_ratio":         ac["silence_ratio"],
        "audio_spectral_flatness":     ac["spectral_flatness"],
        "audio_lip_sync_corr":         ac["lip_sync_corr"],
    }


async def compute_risk_score(session_data: dict) -> dict:
    """
    Aggregate all verification findings into a multidimensional risk score.

    Args:
        session_data: Dict containing document_result, biometric_result,
                      sanctions_result, and onboarding_context.

    Returns:
        dict with keys: risk_level ("low"|"medium"|"high"), score (float 0–1),
                        triggers (list of contributing factors)
    """
    return await _compute_risk(session_data)
