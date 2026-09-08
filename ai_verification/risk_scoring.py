"""
risk_scoring.py
===============
Aggregate verification findings into a KYC risk score and band.

Scoring is deterministic and rule-based. Signals from the document, biometric,
session-quality, temporal-consistency and audio-consistency checks each carry a
fixed weight; the weighted sum maps onto three bands:

  score < 0.33   low     auto-approve, issue VC
  score < 0.67   medium  approve with monitoring
  score >= 0.67  high    reject, route to Enhanced Due Diligence

Three checks act as standalone gates rather than weighted terms: a document that
fails authentication, a face that does not match the document portrait, and a
failed liveness check each set hard_fail on their own. A pass on every other
signal must not average these out.

The band boundaries are those used in the thesis validation runs, so a session
scored here is directly comparable to the reported results.

A learned model over the same signals would replace the fixed weights, but that
needs labelled onboarding outcomes, which were not available for this work. The
band boundaries and the return schema would be unchanged, so swapping one in is
a local change to _rule_based_score.
"""

from __future__ import annotations


def _rule_based_score(session_data: dict) -> tuple[float, list[str], bool]:
    """
    Simple deterministic heuristic — replace with a trained model.

    Returns:
        (score: float 0–1, triggers: list of contributing factor strings,
         hard_fail: True if a disqualifying check failed outright)
    """
    score     = 0.0
    triggers  = []
    hard_fail = False

    doc = session_data.get("document_result", {}) or {}
    bio = session_data.get("biometric_result", {}) or {}

    # Disqualifying checks: each is a standalone security gate. A pass on
    # every other signal must not average these out — a forged document,
    # an identity mismatch, or a failed anti-spoofing check is rejected
    # on its own, not blended into a 0-1 score with everything else.
    if not doc.get("authentic", True):
        score += 0.40
        triggers.append("document_not_authentic")
        hard_fail = True

    if not bio.get("face_match", True):
        score += 0.30
        triggers.append("face_mismatch")
        hard_fail = True

    if not bio.get("liveness_pass", True):
        score += 0.40
        triggers.append("liveness_failed")
        hard_fail = True

    # Non-disqualifying signals: these only modulate severity within the
    # "passed the hard gates" tier (low vs. medium).
    confidence = doc.get("confidence", 1.0)
    if confidence < 0.70:
        score += 0.20
        triggers.append("low_document_confidence")

    flags = doc.get("flags", [])
    if flags:
        score += 0.10 * min(len(flags), 3)
        triggers.append(f"document_flags:{','.join(flags[:3])}")

    # Etapa 1 — Session quality (low fps, low resolution, too short)
    if bio.get("session_quality_pass") is False:
        score += 0.15
        sq_flags = bio.get("session_quality_flags", [])
        triggers.append(f"session_quality_failed:{','.join(sq_flags)}")

    # Etapa 6 — Temporal consistency (frame jumps, frozen video)
    if bio.get("temporal_consistency_pass") is False:
        score += 0.20
        tc_flags = bio.get("temporal_consistency_flags", [])
        triggers.append(f"temporal_consistency_failed:{','.join(tc_flags)}")

    # Etapa 7 — Audio consistency (no audio, flat spectrum, poor lip sync)
    if bio.get("audio_consistency_pass") is False:
        score += 0.20
        ac_flags = bio.get("audio_consistency_flags", [])
        triggers.append(f"audio_consistency_failed:{','.join(ac_flags)}")
        # Missing audio track entirely is a hard signal
        if not bio.get("audio_has_audio", True):
            hard_fail = True

    # Etapa 8 — Contextual verification (knowledge-based challenge)
    challenge_pass = session_data.get("challenge_pass")
    if challenge_pass is False:
        score += 0.25
        triggers.append("contextual_challenge_failed")
        hard_fail = True

    return min(score, 1.0), triggers, hard_fail


async def compute_risk_score(session_data: dict) -> dict:
    """
    Compute the onboarding risk score.

    Args:
        session_data: Dict containing:
            - document_result  (from document_auth.authenticate_document)
            - biometric_result (from face_verification + liveness)
            - (optional) sanctions_result, pep_flag, nationality

    Returns:
        {
            "risk_level": "low" | "medium" | "high",
            "score":      float,   # 0.0 = no risk, 1.0 = maximum risk
            "triggers":   list,    # contributing factors
        }
    """
    score, triggers, hard_fail = _rule_based_score(session_data)

    if hard_fail:
        level = "high"
    elif score < 0.33:
        level = "low"
    elif score < 0.67:
        level = "medium"
    else:
        level = "high"

    return {"risk_level": level, "score": round(score, 4), "triggers": triggers}
