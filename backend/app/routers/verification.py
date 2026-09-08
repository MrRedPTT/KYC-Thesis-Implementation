from fastapi import APIRouter, HTTPException

from app.models.schemas import VerifyCredentialRequest, VerifyCredentialResponse
from app.services.blockchain_service import verify_credential, record_audit_event

router = APIRouter()


@router.post("/credential", response_model=VerifyCredentialResponse)
async def verify_vc(request: VerifyCredentialRequest):
    """
    Verify a Verifiable Credential presented by a Holder (the "Bank B" reuse flow).

    Checks:
      1. The credential hash matches the on-chain record (tamper-proof).
      2. The credential is Active (not revoked or suspended).
      3. The credential has not expired.
    """
    result = await verify_credential(request.credential_id, request.credential_hash)

    await record_audit_event(
        subject_did="did:verifier:bank-b",
        event_type_name="CredentialReused",
        payload={
            "credential_id": request.credential_id,
            "valid":         result["valid"],
            "status":        result["status"],
        },
    )

    if not result["valid"]:
        raise HTTPException(
            status_code=400,
            detail=f"Credential is invalid. On-chain status: {result['status']}",
        )
    return VerifyCredentialResponse(**result)
