"""
face_verification.py
====================
1:1 face verification using InsightFace ArcFace (buffalo_l model pack).

Pipeline:
  1. Decode both images from raw bytes with OpenCV.
  2. Detect the largest face in each image using InsightFace's SCRFD detector.
  3. Extract a 512-dimensional ArcFace embedding for each face.
  4. Compute the cosine distance between the two embeddings.
  5. Classify as MATCH if the distance is below COSINE_THRESHOLD.

Model: buffalo_l (NIST-FRVT rank-1 accuracy).
       Downloaded automatically to ~/.insightface/models/buffalo_l/ on first run.

Rationale for InsightFace over DeepFace:
  InsightFace was the reference tool used throughout the thesis experimental
  evaluation (Chapter ExperimentalResults). Using the same model pack ensures
  that match thresholds calibrated during experiments are directly applicable
  to the prototype.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

# InsightFace is imported lazily so the module can be imported without GPU/model
# download at test time.
_app = None


def _get_app():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis

        _app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))  # ctx_id=-1 → CPU
    return _app


# Cosine distance threshold: ≤ threshold → same person.
# Calibrated from the thesis InsightFace experiments (buffalo_l on frontal faces).
COSINE_THRESHOLD: float = 0.40


def _decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode raw image bytes (JPEG / PNG / BMP) to a BGR NumPy array."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        # Fallback via Pillow (handles more formats, e.g. WebP)
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return img


def _largest_embedding(img: np.ndarray) -> np.ndarray | None:
    """Return the ArcFace embedding for the largest detected face, or None."""
    app = _get_app()
    faces = app.get(img)
    if not faces:
        return None
    largest = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return largest.normed_embedding  # already L2-normalised by InsightFace


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2]. 0 = identical, 2 = opposite."""
    return float(1.0 - np.dot(a, b))


async def verify_face(live_image_bytes: bytes, reference_image_bytes: bytes) -> dict:
    """
    Perform 1:1 face verification.

    Args:
        live_image_bytes:      JPEG / PNG selfie or video frame from the user.
        reference_image_bytes: Portrait crop extracted from the identity document.
                               Pass an empty bytes object (b"") to skip face matching
                               (returns match=False, score=1.0).

    Returns:
        {
            "match":  bool,   # True if cosine distance ≤ COSINE_THRESHOLD
            "score":  float,  # cosine distance (0.0 = identical, 1.0 = unrelated)
            "detail": str,    # human-readable explanation
        }

    Raises:
        ValueError: If no face is detected in the live image.
    """
    # Portrait not yet available (e.g. document_auth stub still in use)
    if not reference_image_bytes:
        return {
            "match":  False,
            "score":  1.0,
            "detail": "No reference portrait supplied; face matching skipped.",
        }

    live_img = _decode_image(live_image_bytes)
    ref_img  = _decode_image(reference_image_bytes)

    live_emb = _largest_embedding(live_img)
    if live_emb is None:
        raise ValueError("No face detected in the submitted live image.")

    ref_emb = _largest_embedding(ref_img)
    if ref_emb is None:
        return {
            "match":  False,
            "score":  1.0,
            "detail": "No face detected in the reference document portrait.",
        }

    distance = _cosine_distance(live_emb, ref_emb)
    match    = distance <= COSINE_THRESHOLD

    return {
        "match":  match,
        "score":  round(distance, 4),
        "detail": (
            f"Cosine distance {distance:.4f} "
            f"({'≤' if match else '>'} threshold {COSINE_THRESHOLD}) — "
            f"{'MATCH' if match else 'NO MATCH'}."
        ),
    }
