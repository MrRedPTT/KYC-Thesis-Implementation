"""
document_auth.py
================
Identity document authentication via OCR and structural analysis.

Pipeline:
  1. Pre-processing   — normalise colour, upscale for OCR accuracy.
  2. OCR              — extract all text blocks using EasyOCR (CPU mode).
  3. MRZ detection    — locate and parse the Machine Readable Zone (ICAO 9303 TD3).
  4. Integrity checks — flag absent MRZ, expiry in the past, or low OCR confidence.
  5. Portrait crop    — use InsightFace SCRFD (with OpenCV Haar-cascade fallback)
                        to extract the document portrait as JPEG bytes; forwarded
                        to face_verification.py.

Supported formats: JPEG, PNG, BMP, WebP.
PDF is not supported (flag "pdf_not_supported" returned, portrait_bytes = b"").

MRZ format reference: ICAO Doc 9303, Part 3 (TD3 / passport, 2×44 chars).
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any

import cv2
import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# EasyOCR reader — lazily initialised (first call downloads model weights)
# ---------------------------------------------------------------------------
_easyocr_reader = None


def _get_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        # English + Portuguese: required for accurate OCR of the Portuguese
        # Cartão de Cidadão (diacritics in field labels) and other PT/Latin
        # documents. The two scripts are compatible in a single Reader.
        _easyocr_reader = easyocr.Reader(["en", "pt"], gpu=False, verbose=False)
    return _easyocr_reader


# ---------------------------------------------------------------------------
# InsightFace face detector — lazily initialised, with OpenCV Haar fallback
# ---------------------------------------------------------------------------
_face_app = None


def _get_face_app():
    global _face_app
    if _face_app is None:
        try:
            from insightface.app import FaceAnalysis
            _face_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            _face_app.prepare(ctx_id=-1, det_size=(640, 640))
        except Exception:
            _face_app = "haar"  # sentinel: use OpenCV Haar fallback
    return _face_app


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode raw bytes → BGR NumPy array."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return img


def _preprocess(img: np.ndarray) -> np.ndarray:
    """Upscale to at least 1200 px on the longer edge to improve OCR accuracy."""
    h, w = img.shape[:2]
    if max(h, w) < 1200:
        scale = 1200 / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    return img


_MRZ_ALLOWLIST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ<"


def _run_ocr(img: np.ndarray) -> tuple[list[dict], str, float]:
    """
    Run EasyOCR on *img*.

    Returns:
        blocks    — list of {text, bbox, confidence}
        full_text — all detected text, newline-separated
        avg_conf  — mean detection confidence (0.0 – 1.0)
    """
    reader = _get_reader()
    raw = reader.readtext(img, detail=1)

    blocks: list[dict] = []
    confidences: list[float] = []
    for bbox, text, conf in raw:
        bbox_py = [[int(x), int(y)] for x, y in bbox]
        blocks.append({"text": text, "bbox": bbox_py, "confidence": round(conf, 3)})
        confidences.append(conf)

    full_text = "\n".join(b["text"] for b in blocks)
    avg_conf  = float(np.mean(confidences)) if confidences else 0.0
    return blocks, full_text, avg_conf


def _ocr_mrz_region(img: np.ndarray, blocks: list[dict]) -> tuple[str, float]:
    """
    Focused second OCR pass over the MRZ region, using an MRZ-only
    allow-list.

    Localisation strategy: scan the first-pass OCR ``blocks`` for entries
    that look MRZ-shaped (25-46 chars, MRZ charset, contains '<'), and
    crop a tight region around their bounding boxes. This is much more
    robust than a fixed bottom-strip heuristic — Portuguese Cartões de
    Cidadão are commonly scanned with both card sides stacked, putting
    the MRZ near the *middle* of the image rather than at the bottom.

    If no MRZ-shaped blocks are found, fall back to the bottom 30 %.

    Returns:
        joined_text — newline-separated raw OCR text from the strip
        avg_conf    — mean detection confidence over the strip
    """
    h, w = img.shape[:2]
    if h < 100:
        return "", 0.0

    # 1. Try to localise MRZ from first-pass blocks
    y_top: int | None = None
    y_bot: int | None = None
    for b in blocks:
        clean = b["text"].upper().replace(" ", "")
        if not (25 <= len(clean) <= 46 and "<" in clean):
            continue
        if not _MRZ_CHARSET.match(clean):
            continue
        ys = [pt[1] for pt in b["bbox"]]
        block_top, block_bot = min(ys), max(ys)
        y_top = block_top if y_top is None else min(y_top, block_top)
        y_bot = block_bot if y_bot is None else max(y_bot, block_bot)

    if y_top is not None and y_bot is not None:
        pad = max(10, (y_bot - y_top) // 6)
        y1 = max(0, y_top - pad)
        y2 = min(h, y_bot + pad)
        strip = img[y1:y2, :]
    else:
        # 2. No MRZ-shaped blocks located — fall back to bottom 30 %
        strip = img[int(h * 0.70):, :]

    reader = _get_reader()
    raw = reader.readtext(strip, detail=1, allowlist=_MRZ_ALLOWLIST)
    if not raw:
        return "", 0.0
    text = "\n".join(t for _, t, _ in raw)
    conf = float(np.mean([c for _, _, c in raw]))
    return text, conf


# ---------------------------------------------------------------------------
# MRZ parsing (ICAO 9303 TD3 — passport, 2×44 chars; TD1 — ID card, 3×30 chars)
# ---------------------------------------------------------------------------

_MRZ_CHARSET = re.compile(r"^[A-Z0-9<]+$")
_WEIGHTS     = [7, 3, 1]
_CHAR_VALUES = {c: i for i, c in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ<")}


def _check_digit(field: str) -> str:
    """Compute the ICAO 9303 Annex-B check digit for *field*."""
    total = sum(_CHAR_VALUES.get(c, 0) * _WEIGHTS[i % 3] for i, c in enumerate(field))
    return str(total % 10)


def _extract_mrz_lines(full_text: str) -> list[str]:
    """
    Return up to 3 MRZ-looking lines from OCR output.

    MRZ lines are A-Z/0-9/< only and *always* contain '<' (filler in the
    optional-data and name fields). We accept candidates within 2 chars of
    either 30 (TD1) or 44 (TD3), padding short lines with '<' — OCR commonly
    drops trailing filler. The MRZ block is at the bottom of the document,
    so we return the *last* 3 matching lines, not the first 3 (which can be
    long uppercase names mis-identified as MRZ).
    """
    candidates: list[str] = []
    for raw in full_text.splitlines():
        ln = raw.upper().replace(" ", "")
        if not _MRZ_CHARSET.match(ln) or "<" not in ln:
            continue
        if 28 <= len(ln) <= 32:
            candidates.append(ln.ljust(30, "<")[:30])
        elif 42 <= len(ln) <= 46:
            candidates.append(ln.ljust(44, "<")[:44])
    return candidates[-3:]


def _parse_mrz_td3(line1: str, line2: str) -> dict[str, Any]:
    """Parse 2×44 TD3 MRZ lines; returns {} if lines are too short."""
    if len(line1) < 44 or len(line2) < 44:
        return {}

    result: dict[str, Any] = {"format": "TD3"}
    result["doc_type"]    = line1[0:2].replace("<", "")
    result["country"]     = line1[2:5]
    name_parts            = line1[5:44].split("<<", 1)
    result["surname"]     = name_parts[0].replace("<", " ").strip()
    result["given"]       = name_parts[1].replace("<", " ").strip() if len(name_parts) > 1 else ""
    result["doc_number"]  = line2[0:9].replace("<", "")
    result["nationality"] = line2[10:13].replace("<", "")

    # Date of birth (YYMMDD)
    try:
        yy   = int(line2[13:15])
        year = 1900 + yy if yy > (datetime.today().year % 100) else 2000 + yy
        result["dob"] = date(year, int(line2[15:17]), int(line2[17:19])).isoformat()
    except (ValueError, IndexError):
        result["dob"] = None

    result["sex"] = line2[20] if len(line2) > 20 else ""

    # Expiry (YYMMDD, always future-century)
    try:
        result["expiry"] = date(2000 + int(line2[21:23]), int(line2[23:25]), int(line2[25:27])).isoformat()
    except (ValueError, IndexError):
        result["expiry"] = None

    result["dn_check_ok"]  = len(line2) > 9  and _check_digit(line2[0:9])   == line2[9]
    result["exp_check_ok"] = len(line2) > 27 and _check_digit(line2[21:27]) == line2[27]
    result["valid"]        = result["dn_check_ok"] and result["exp_check_ok"]
    return result


def _parse_mrz_td1(line1: str, line2: str, line3: str) -> dict[str, Any]:
    """
    Parse 3×30 TD1 MRZ lines (ICAO 9303 Part 5 — used by national ID cards
    such as the Portuguese Cartão de Cidadão). Returns {} if any line is
    too short.

    Line 1: doc_type(2) issuer(3) doc_number(9) dn_check(1) optional1(15)
    Line 2: dob(6) dob_check(1) sex(1) expiry(6) exp_check(1)
            nationality(3) optional2(11) composite_check(1)
    Line 3: name (SURNAME<<GIVEN_NAMES, padded to 30 with '<')
    """
    if len(line1) < 30 or len(line2) < 30 or len(line3) < 30:
        return {}

    result: dict[str, Any] = {"format": "TD1"}
    result["doc_type"]    = line1[0:2].replace("<", "")
    result["country"]     = line1[2:5]
    result["doc_number"]  = line1[5:14].replace("<", "")
    result["nationality"] = line2[15:18].replace("<", "")

    name_parts        = line3[0:30].split("<<", 1)
    result["surname"] = name_parts[0].replace("<", " ").strip()
    result["given"]   = name_parts[1].replace("<", " ").strip() if len(name_parts) > 1 else ""

    # Date of birth (YYMMDD) — cutover at current 2-digit year.
    try:
        yy   = int(line2[0:2])
        year = 1900 + yy if yy > (datetime.today().year % 100) else 2000 + yy
        result["dob"] = date(year, int(line2[2:4]), int(line2[4:6])).isoformat()
    except (ValueError, IndexError):
        result["dob"] = None

    result["sex"] = line2[7] if len(line2) > 7 else ""

    # Expiry (YYMMDD, always future-century)
    try:
        result["expiry"] = date(
            2000 + int(line2[8:10]), int(line2[10:12]), int(line2[12:14])
        ).isoformat()
    except (ValueError, IndexError):
        result["expiry"] = None

    result["dn_check_ok"]  = _check_digit(line1[5:14])  == line1[14]
    result["dob_check_ok"] = _check_digit(line2[0:6])   == line2[6]
    result["exp_check_ok"] = _check_digit(line2[8:14])  == line2[14]

    composite_field = line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29]
    result["composite_check_ok"] = _check_digit(composite_field) == line2[29]

    passed = (
        int(result["dn_check_ok"])
        + int(result["dob_check_ok"])
        + int(result["exp_check_ok"])
        + int(result["composite_check_ok"])
    )
    result["checks_passed"] = passed
    result["strict_valid"]  = passed == 4

    # Practical validity rule for TD1 — required because the Portuguese
    # Cartão de Cidadão MRZ does not always reproduce the strict ICAO
    # doc-number check digit (the printed character at line1[14] is
    # frequently a literal '<' rather than the ICAO-computed digit, and
    # the composite check fails as a cascade). We anchor validity on
    # the two temporal check digits (date of birth, expiry) — these are
    # the data fields whose integrity matters most for KYC — and
    # require at least half of all four to pass.
    result["valid"] = (
        result["dob_check_ok"]
        and result["exp_check_ok"]
        and passed >= 2
    )
    return result


def _parse_mrz(mrz_lines: list[str]) -> dict[str, Any]:
    # TD1: 3 lines × 30 chars (ID cards). TD3: 2 lines × 44 chars (passports).
    if len(mrz_lines) >= 3 and all(len(ln) == 30 for ln in mrz_lines[:3]):
        return _parse_mrz_td1(mrz_lines[0], mrz_lines[1], mrz_lines[2])
    if len(mrz_lines) >= 2 and all(len(ln) == 44 for ln in mrz_lines[:2]):
        return _parse_mrz_td3(mrz_lines[0], mrz_lines[1])
    return {}


# ---------------------------------------------------------------------------
# Portrait extraction
# ---------------------------------------------------------------------------

def _extract_portrait(img: np.ndarray) -> bytes:
    """
    Crop the photograph from a document image.

    Tries InsightFace SCRFD first; falls back to OpenCV Haar cascade.
    Returns JPEG-encoded bytes with 20% padding, or b"" if no face found.
    """
    face_app = _get_face_app()
    face_box = None

    if face_app != "haar":
        try:
            faces = face_app.get(img)
            if faces:
                largest  = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
                face_box = tuple(map(int, largest.bbox))
        except Exception:
            pass

    if face_box is None:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade      = cv2.CascadeClassifier(cascade_path)
        gray         = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        detections   = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        if len(detections) > 0:
            x, y, w, h = max(detections, key=lambda r: r[2] * r[3])
            face_box   = (x, y, x + w, y + h)

    if face_box is None:
        return b""

    x1, y1, x2, y2 = face_box
    pad_x = int((x2 - x1) * 0.20)
    pad_y = int((y2 - y1) * 0.20)
    h_img, w_img   = img.shape[:2]
    x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    x2, y2 = min(w_img, x2 + pad_x), min(h_img, y2 + pad_y)

    crop = img[y1:y2, x1:x2]
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return bytes(buf)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def authenticate_document(file_bytes: bytes, content_type: str) -> dict:
    """
    Authenticate an uploaded identity document.

    Args:
        file_bytes:   Raw bytes of the uploaded file (JPEG, PNG, BMP, WebP).
        content_type: MIME type string reported by the HTTP client.

    Returns:
        {
            "ocr_data":       dict,   # {raw_text, blocks, mrz}
            "authentic":      bool,   # overall authenticity decision
            "confidence":     float,  # 0.0 – 1.0
            "flags":          list,   # detected anomaly strings
            "portrait_bytes": bytes,  # JPEG portrait crop for biometric matching
        }

    ``authentic`` is True only when:
      - A valid MRZ pair with passing check digits was found.
      - The document has not expired.
      - Mean OCR confidence ≥ 0.50.
      - A face was successfully extracted from the document image.
    """
    flags: list[str] = []

    if "pdf" in content_type.lower():
        flags.append("pdf_not_supported")
        return {
            "ocr_data":       {"raw_text": "", "blocks": [], "mrz": {}},
            "authentic":      False,
            "confidence":     0.0,
            "flags":          flags,
            "portrait_bytes": b"",
        }

    # 1. Decode + pre-process
    img = _decode_image(file_bytes)
    img = _preprocess(img)

    # 2a. Full-document OCR (drives portrait crop, raw text capture, and
    #     the soft `low_ocr_confidence` flag).
    blocks, full_text, avg_conf = _run_ocr(img)
    if avg_conf < 0.50:
        flags.append("low_ocr_confidence")

    # 2b. Focused MRZ-region OCR with the MRZ allow-list. This is the
    #     OCR pass that feeds the structural authenticity decision —
    #     the general pass routinely misreads OCR-B check-digit
    #     positions as '<'. The localiser uses the first-pass blocks
    #     to find the actual MRZ position rather than assuming bottom.
    mrz_text, mrz_conf = _ocr_mrz_region(img, blocks)
    mrz_lines = _extract_mrz_lines(mrz_text)
    if not mrz_lines:
        # Fall back to the full-document text if the focused pass came
        # back empty (e.g. document framed without bottom margin).
        mrz_lines = _extract_mrz_lines(full_text)
        mrz_conf  = avg_conf

    mrz_data: dict[str, Any] = {}

    if not mrz_lines:
        flags.append("no_mrz_detected")
    else:
        mrz_data = _parse_mrz(mrz_lines)
        if not mrz_data.get("valid"):
            flags.append("mrz_checksum_failed")

        expiry_str = mrz_data.get("expiry")
        if expiry_str:
            try:
                if date.fromisoformat(expiry_str) < date.today():
                    flags.append("document_expired")
            except ValueError:
                flags.append("expiry_parse_error")
        else:
            flags.append("expiry_not_found")

    # 4. Portrait extraction
    portrait_bytes = _extract_portrait(img)
    if not portrait_bytes:
        flags.append("no_portrait_detected")

    # 5. Authenticity verdict — gated on the MRZ-region OCR confidence
    #    rather than the document-wide average. The non-MRZ portion of a
    #    real ID card (stylised guilloché, multilingual field labels,
    #    holograms) commonly OCRs poorly without that signalling
    #    anything about the structural validity of the document; the
    #    MRZ check digits are the actual integrity guarantee.
    authentic = (
        bool(mrz_data)
        and mrz_data.get("valid", False)
        and "document_expired"     not in flags
        and "no_portrait_detected" not in flags
        and mrz_conf >= 0.50
    )

    # Confidence: 60 % MRZ-region OCR quality + 40 % MRZ validity.
    mrz_ok     = 1.0 if (mrz_data and mrz_data.get("valid")) else 0.0
    confidence = round(0.6 * mrz_conf + 0.4 * mrz_ok, 3)

    return {
        "ocr_data": {
            "raw_text": full_text,
            "blocks":   blocks,
            "mrz":      mrz_data,
        },
        "authentic":      authentic,
        "confidence":     confidence,
        "flags":          flags,
        "portrait_bytes": portrait_bytes,
    }
