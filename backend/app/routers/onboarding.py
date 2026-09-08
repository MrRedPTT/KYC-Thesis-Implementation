import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException
from web3 import Web3

from app.models.schemas import (
    OnboardingStartRequest,
    OnboardingStartResponse,
    DocumentVerificationResult,
    BiometricVerificationResult,
    OnboardingCompleteResponse,
    ChallengeResponse,
    ChallengeAnswerRequest,
    ChallengeAnswerResponse,
    RiskLevel,
)
from app.services.ai_service        import verify_document, verify_biometrics, compute_risk_score
from app.services.blockchain_service import issue_credential, record_audit_event
from app.utils.credential_id        import make_credential_id

router = APIRouter()


def _derive_holder_did(holder_address: str) -> str:
    """
    Derive a W3C did:ethr identifier from an Ethereum address.

    The did:ethr method specification defines the DID as
    ``did:ethr:<checksum-address>`` on the default chain.
    """
    if not Web3.is_address(holder_address):
        raise HTTPException(
            status_code=400,
            detail=f"holder_address is not a valid Ethereum address: {holder_address!r}",
        )
    return f"did:ethr:{Web3.to_checksum_address(holder_address)}"

# In-memory session store (replace with Redis or DB in production)
_sessions: dict[str, dict] = {}


def _build_challenge(mrz: dict) -> tuple[str, str] | None:
    """
    Derive a knowledge-based question from MRZ fields.

    Returns (question, expected_answer) or None if insufficient data.
    The answer is normalised to uppercase with whitespace stripped.
    """
    doc_number = mrz.get("doc_number", "")
    if len(doc_number) >= 3:
        return (
            "What are the last 3 characters of your document number?",
            doc_number[-3:].upper(),
        )
    dob = mrz.get("dob")  # "YYYY-MM-DD"
    if dob:
        try:
            month = str(int(dob.split("-")[1]))
            return (
                "What is the number of your birth month? (1 = January, 12 = December)",
                month,
            )
        except (IndexError, ValueError):
            pass
    return None


@router.post("/start", response_model=OnboardingStartResponse)
async def start_onboarding(request: OnboardingStartRequest):
    """
    Initiate a new KYC onboarding session.

    The caller must supply the holder's Ethereum address; the backend
    derives a did:ethr DID from it and uses that DID as the audit-trail
    subject for every subsequent event in this session.

    Returns a session_id that must be included in subsequent calls.
    """
    holder_did = _derive_holder_did(request.holder_address)
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "request":          request.model_dump(),
        "holder_did":       holder_did,
        "document_result":  None,
        "biometric_result": None,
    }
    await record_audit_event(
        subject_did=holder_did,
        event_type_name="OnboardingStarted",
        payload={"session_id": session_id, "nationality": request.nationality},
    )
    return OnboardingStartResponse(session_id=session_id, holder_did=holder_did, status="started")


@router.post("/{session_id}/document", response_model=DocumentVerificationResult)
async def upload_document(session_id: str, file: UploadFile = File(...)):
    """
    Accept an identity document and run OCR + structural authentication.
    Supported formats: image/jpeg, image/png, application/pdf
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    content = await file.read()
    result  = await verify_document(content, file.content_type)

    _sessions[session_id]["document_result"] = result

    # Etapa 8 — generate contextual challenge from MRZ data while it's fresh.
    mrz = result.get("ocr_data", {}).get("mrz", {})
    challenge = _build_challenge(mrz)
    if challenge:
        _sessions[session_id]["challenge_question"] = challenge[0]
        _sessions[session_id]["challenge_expected"] = challenge[1]
        _sessions[session_id]["challenge_pass"]     = None  # not yet answered
    else:
        _sessions[session_id]["challenge_pass"] = None  # no MRZ — skip penalty

    await record_audit_event(
        subject_did=_sessions[session_id]["holder_did"],
        event_type_name="DocumentVerified",
        payload={"authentic": result["authentic"], "confidence": result["confidence"]},
    )
    return DocumentVerificationResult(session_id=session_id, **result)


@router.get("/{session_id}/challenge", response_model=ChallengeResponse)
async def get_challenge(session_id: str):
    """
    Etapa 8 — Return the contextual knowledge-based challenge for this session.

    The question is derived from the MRZ fields extracted during document
    upload. Returns available=False when insufficient MRZ data was found.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    if session.get("document_result") is None:
        raise HTTPException(status_code=400, detail="Document must be submitted first")

    question = session.get("challenge_question")
    return ChallengeResponse(
        session_id=session_id,
        available=question is not None,
        question=question,
    )


@router.post("/{session_id}/challenge", response_model=ChallengeAnswerResponse)
async def submit_challenge_answer(session_id: str, body: ChallengeAnswerRequest):
    """
    Etapa 8 — Submit the answer to the contextual challenge.

    The answer is compared (case-insensitive, whitespace-stripped) against
    the value derived from the MRZ. A wrong answer is recorded and feeds
    into the final risk score, but does not block the biometric step.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    if session.get("document_result") is None:
        raise HTTPException(status_code=400, detail="Document must be submitted first")

    expected = session.get("challenge_expected")
    if expected is None:
        return ChallengeAnswerResponse(
            session_id=session_id,
            correct=True,
            detail="No challenge available for this session — skipped.",
        )

    submitted = body.answer.strip().upper()
    correct   = submitted == expected.upper()
    _sessions[session_id]["challenge_pass"] = correct

    await record_audit_event(
        subject_did=session["holder_did"],
        event_type_name="ChallengeVerified",
        payload={"correct": correct},
    )

    detail = "Challenge passed." if correct else "Incorrect answer — flagged for risk scoring."
    return ChallengeAnswerResponse(session_id=session_id, correct=correct, detail=detail)


@router.post("/{session_id}/biometric", response_model=BiometricVerificationResult)
async def submit_biometric(session_id: str, file: UploadFile = File(...)):
    """
    Accept a short video clip (MP4, WebM, AVI — minimum ~4 s at 30 fps).
    Runs ArcFace face 1:1 matching against the document portrait and
    rPPG-POS liveness detection. A single still image will be rejected
    by the liveness module (insufficient frames).
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    doc_result = _sessions[session_id].get("document_result")
    if doc_result is None:
        raise HTTPException(status_code=400, detail="Document must be submitted first")

    content         = await file.read()
    reference_image = doc_result.get("portrait_bytes", b"")  # extracted by document_auth

    result = await verify_biometrics(content, reference_image)

    _sessions[session_id]["biometric_result"] = result
    await record_audit_event(
        subject_did=_sessions[session_id]["holder_did"],
        event_type_name="BiometricVerified",
        payload={
            "face_match":    result["face_match"],
            "liveness_pass": result["liveness_pass"],
        },
    )
    return BiometricVerificationResult(session_id=session_id, **result)


@router.post("/{session_id}/complete", response_model=OnboardingCompleteResponse)
async def complete_onboarding(session_id: str):
    """
    Finalise KYC: compute risk score and — if approved — issue a
    Verifiable Credential anchored on the blockchain.

    The holder DID (did:ethr) was fixed at /start and is read from the
    session; no separate parameter is required here.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    if not session["document_result"] or not session["biometric_result"]:
        raise HTTPException(status_code=400, detail="Complete document and biometric steps first")

    holder_did = session["holder_did"]

    risk = await compute_risk_score(session)
    await record_audit_event(
        subject_did=holder_did,
        event_type_name="RiskAssessed",
        payload={"risk_level": risk["risk_level"], "score": risk["score"]},
    )

    approved = risk["risk_level"] in (RiskLevel.LOW, RiskLevel.MEDIUM)
    credential_id = None

    if approved:
        import json, time
        vc_json = json.dumps({
            "@context":          ["https://www.w3.org/2018/credentials/v1"],
            "type":              ["VerifiableCredential", "KYCCredential"],
            "issuer":            "bank-a",
            "issuanceDate":      time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "credentialSubject": {"id": holder_did, **session["request"]},
        }, sort_keys=True)

        cred_id_bytes, credential_id = make_credential_id()
        expires_at = int(time.time()) + 3 * 365 * 24 * 3600  # 3-year validity
        await issue_credential(cred_id_bytes, vc_json, holder_did, "KYCCredential", expires_at)

        await record_audit_event(
            subject_did=holder_did,
            event_type_name="CredentialIssued",
            payload={"credential_id": credential_id},
        )
        event_type = "OnboardingCompleted"
        detail     = "KYC approved. Verifiable Credential issued."
    else:
        event_type = "OnboardingRejected"
        detail     = f"KYC requires Enhanced Due Diligence (EDD). Risk: {risk['risk_level']}"

    await record_audit_event(
        subject_did=holder_did,
        event_type_name=event_type,
        payload={"session_id": session_id},
    )
    del _sessions[session_id]

    return OnboardingCompleteResponse(
        session_id=session_id,
        risk_level=risk["risk_level"],
        approved=approved,
        credential_id=credential_id,
        detail=detail,
    )
