from fastapi import APIRouter, HTTPException

from app.models.schemas import CreateDIDRequest, DIDDocumentResponse
from app.services.blockchain_service import create_did, resolve_did

router = APIRouter()


@router.post("/did", response_model=DIDDocumentResponse, status_code=201)
async def register_did(request: CreateDIDRequest):
    """
    Register a new Decentralized Identifier on the blockchain.
    Called once per user after successful KYC completion.
    """
    doc = await create_did(request.did, request.public_key, request.service_endpoint)
    if not doc:
        raise HTTPException(status_code=500, detail="DID registration failed")
    return DIDDocumentResponse(**doc)


@router.get("/did/{did:path}", response_model=DIDDocumentResponse)
async def get_did(did: str):
    """
    Resolve a DID to its on-chain DID Document.
    Used by Verifiers to retrieve the Issuer's public key for signature verification.
    """
    doc = await resolve_did(did)
    if not doc:
        raise HTTPException(status_code=404, detail="DID not found")
    return DIDDocumentResponse(**doc)
