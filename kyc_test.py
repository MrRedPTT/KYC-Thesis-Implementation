#!/usr/bin/env python3
"""
kyc_test.py — end-to-end KYC onboarding runner (no frontend needed).

Requires: requests  (available in the project venv via web3's dependencies)

Usage:
    python kyc_test.py [options]

    python kyc_test.py --doc "D:/path/to/id.png" --video "D:/path/to/clip.mp4"
    python kyc_test.py --challenge skip
    python kyc_test.py --challenge "PER"    # pre-supply answer

Options:
    --base-url URL      Backend URL          (default: http://localhost:8000)
    --doc      PATH     Document image       (default: ./samples/document.png)
    --video    PATH     Biometric video      (default: ./samples/video.mp4)
    --name     NAME     Full name            (must match the document)
    --nat      CODE     Nationality          (default: PT)
    --type     TYPE     national_id|passport (default: national_id)
    --address  ADDR     Ethereum address     (default: Hardhat account #1)
    --challenge ANS     Answer or 'skip'
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not found. Activate the project venv first:")
    print("  .venv\\Scripts\\activate   (Windows)")
    sys.exit(1)

# ── ANSI colours (Windows 10+ supports them in most terminals) ───────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
GRAY   = "\033[90m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _section(title: str) -> None:
    bar = "─" * max(1, 54 - len(title))
    print(f"\n{CYAN}{BOLD}── {title} {bar}{RESET}")


def _ok(label: str)   -> None: print(f"  {GREEN}[PASS]{RESET} {label}")
def _fail(label: str) -> None: print(f"  {RED}[FAIL]{RESET} {label}")
def _info(label: str) -> None: print(f"  {GRAY}[INFO]{RESET} {label}")
def _warn(label: str) -> None: print(f"  {YELLOW}[WARN]{RESET} {label}")


def _show(value: bool | None, label: str) -> None:
    if value is True:
        _ok(label)
    elif value is False:
        _fail(label)
    else:
        _info(f"{label} (n/a)")


def _check(r: requests.Response) -> dict:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        print(f"\n  {RED}ERROR {r.status_code}{RESET}: {detail}")
        sys.exit(1)
    return r.json()


# ── Main ─────────────────────────────────────────────────────────────────────

# Defaults point at ./samples/. For repeated local runs, copy this file to
# kyc_test.local.py and edit the defaults there -- that filename is
# git-ignored, so personal paths and data never reach the repository.
DEFAULT_DOC   = os.path.join("samples", "document.png")
DEFAULT_VIDEO = os.path.join("samples", "video.mp4")
DEFAULT_ADDR  = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="End-to-end KYC test runner — no frontend needed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--base-url",  default="http://localhost:8000")
    ap.add_argument("--doc",       default=DEFAULT_DOC,   metavar="PATH")
    ap.add_argument("--video",     default=DEFAULT_VIDEO, metavar="PATH")
    ap.add_argument("--name",      default="Jane Doe")
    ap.add_argument("--nat",       default="PT")
    ap.add_argument("--type",      default="national_id",  dest="doc_type")
    ap.add_argument("--address",   default=DEFAULT_ADDR)
    ap.add_argument("--challenge", default="",  metavar="ANS|skip",
                    help="Pre-supply answer or 'skip'")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    s    = requests.Session()

    # ── Pre-flight ────────────────────────────────────────────────────────────

    _section("Pre-flight")
    import os
    for label, path in (("Document", args.doc), ("Video", args.video)):
        if not os.path.isfile(path):
            _fail(f"{label} not found: {path}")
            sys.exit(1)
        _ok(f"{label}: {path}")

    try:
        health = _check(s.get(f"{base}/health", timeout=5))
    except requests.ConnectionError:
        _fail(f"Backend not reachable at {base}")
        sys.exit(1)
    if health.get("status") != "ok":
        _fail(f"Health returned: {health}")
        sys.exit(1)
    _ok(f"Backend reachable ({base})")

    # ── Step 1: Start ─────────────────────────────────────────────────────────

    _section("Step 1 — Start session")
    start = _check(s.post(f"{base}/api/v1/onboarding/start", json={
        "full_name":      args.name,
        "nationality":    args.nat,
        "document_type":  args.doc_type,
        "holder_address": args.address,
    }))
    session = start["session_id"]
    _ok(f"session_id : {session}")
    _info(f"holder_did : {start['holder_did']}")

    # ── Step 2: Document ──────────────────────────────────────────────────────

    _section("Step 2 — Document upload")
    with open(args.doc, "rb") as fh:
        doc = _check(s.post(
            f"{base}/api/v1/onboarding/{session}/document",
            files={"file": (os.path.basename(args.doc), fh, "image/png")},
        ))
    conf = round(doc.get("confidence", 0), 3)
    _show(doc.get("authentic"), f"Document authentic  (confidence {conf})")
    flags = doc.get("flags", [])
    if flags:
        _warn(f"Flags: {', '.join(flags)}")
    _info(f"OCR fields: {', '.join(doc.get('ocr_data', {}).keys())}")

    # ── Step 2b: Challenge ────────────────────────────────────────────────────

    _section("Step 2b — Contextual challenge (Etapa 8)")
    ch = _check(s.get(f"{base}/api/v1/onboarding/{session}/challenge"))
    if not ch.get("available"):
        _info("No challenge available (insufficient MRZ data) — skipped")
    elif args.challenge.lower() == "skip":
        _warn("Challenge skipped (--challenge skip)")
    else:
        _info(f"Question: {ch['question']}")
        answer = args.challenge or input("  Your answer: ").strip()
        ans = _check(s.post(
            f"{base}/api/v1/onboarding/{session}/challenge",
            json={"answer": answer},
        ))
        _show(ans.get("correct"), "Challenge answer")
        _info(ans.get("detail", ""))

    # ── Step 3: Biometric ─────────────────────────────────────────────────────

    _section("Step 3 — Biometric upload (20–60 s)")
    _info(f"Uploading {args.video} …")
    with open(args.video, "rb") as fh:
        bio = _check(s.post(
            f"{base}/api/v1/onboarding/{session}/biometric",
            files={"file": (os.path.basename(args.video), fh, "video/mp4")},
            timeout=120,
        ))

    print()
    _show(bio.get("face_match"),
          f"Face match            (score {round(bio.get('match_score',0),4)})")
    _show(bio.get("liveness_pass"),
          f"Liveness (rPPG)       (score {round(bio.get('liveness_score',0),4)},"
          f" HR {bio.get('liveness_hr_bpm',0)} bpm)")

    sq_pass = bio.get("session_quality_pass")
    if sq_pass is not None:
        _show(sq_pass, "Session quality")
        sq_flags = bio.get("session_quality_flags", [])
        if sq_flags:
            _warn(f"  Flags: {', '.join(sq_flags)}")

    tc_pass = bio.get("temporal_consistency_pass")
    if tc_pass is not None:
        _show(tc_pass, "Temporal consistency")
        tc_flags = bio.get("temporal_consistency_flags", [])
        if tc_flags:
            _warn(f"  Flags: {', '.join(tc_flags)}")

    ac_pass = bio.get("audio_consistency_pass")
    if ac_pass is not None:
        sil = bio.get("audio_silence_ratio")
        flat = bio.get("audio_spectral_flatness")
        sync = bio.get("audio_lip_sync_corr")
        detail = f"silence {sil}, flatness {flat}"
        if sync is not None:
            detail += f", lip_sync {sync}"
        _show(ac_pass, f"Audio consistency     ({detail})")
        ac_flags = bio.get("audio_consistency_flags", [])
        if ac_flags:
            _warn(f"  Flags: {', '.join(ac_flags)}")

    # ── Step 4: Complete ──────────────────────────────────────────────────────

    _section("Step 4 — Complete onboarding")
    complete = _check(s.post(f"{base}/api/v1/onboarding/{session}/complete"))

    level = complete.get("risk_level", "?").upper()
    colour = {"LOW": GREEN, "MEDIUM": YELLOW, "HIGH": RED}.get(level, RESET)
    print(f"\n  Risk level : {colour}{BOLD}{level}{RESET}")
    _info(f"Detail     : {complete.get('detail')}")

    print()
    cred_id = complete.get("credential_id")
    if cred_id:
        print(f"  {GREEN}{BOLD}SUCCESS — Verifiable Credential issued{RESET}")
        print(f"  {GREEN}credential_id: {cred_id}{RESET}")
    else:
        print(f"  {RED}{BOLD}REJECTED — {complete.get('detail')}{RESET}")
    print()


if __name__ == "__main__":
    main()
