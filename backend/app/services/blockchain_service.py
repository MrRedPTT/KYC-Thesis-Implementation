import os
import json

from web3 import Web3
from pathlib import Path

from app.utils.credential_id import decode_credential_id

# ---------------------------------------------------------------------------
# Web3 connection + contract loading (lazy)
#
# Initialisation is deferred until the first on-chain call so that the
# module can be imported in environments without a configured .env
# (unit tests, CI lint passes, doc generation, etc.).
# ---------------------------------------------------------------------------

_ARTIFACTS_DIR = (
    Path(__file__).resolve().parents[3]   # implementation/
    / "blockchain" / "artifacts" / "contracts"
)

_w3 = None
_account = None
_did_registry = None
_vc_registry = None
_audit_trail = None


def _load_abi(contract_name: str) -> list:
    path = _ARTIFACTS_DIR / f"{contract_name}.sol" / f"{contract_name}.json"
    with open(path) as f:
        return json.load(f)["abi"]


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. "
            "Run `npx hardhat run scripts/deploy.js` and copy the printed "
            "values into implementation/backend/.env."
        )
    return value


def _ensure_initialised() -> None:
    global _w3, _account, _did_registry, _vc_registry, _audit_trail
    if _w3 is not None:
        return

    rpc_url = os.getenv("BLOCKCHAIN_RPC_URL", "http://127.0.0.1:8545")
    _w3 = Web3(Web3.HTTPProvider(rpc_url))
    _account = _w3.eth.account.from_key(_require_env("DEPLOYER_PRIVATE_KEY"))

    _did_registry = _w3.eth.contract(
        address=Web3.to_checksum_address(_require_env("DID_REGISTRY_ADDRESS")),
        abi=_load_abi("DIDRegistry"),
    )
    _vc_registry = _w3.eth.contract(
        address=Web3.to_checksum_address(_require_env("VC_REGISTRY_ADDRESS")),
        abi=_load_abi("VCRegistry"),
    )
    _audit_trail = _w3.eth.contract(
        address=Web3.to_checksum_address(_require_env("AUDIT_TRAIL_ADDRESS")),
        abi=_load_abi("AuditTrail"),
    )


# ---------------------------------------------------------------------------
# Helper: build, sign, and send a transaction
# ---------------------------------------------------------------------------

def _send_tx(contract_fn):
    _ensure_initialised()
    tx = contract_fn.build_transaction({
        "from":  _account.address,
        "nonce": _w3.eth.get_transaction_count(_account.address),
        "gas":   500_000,
    })
    signed   = _account.sign_transaction(tx)
    tx_hash  = _w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt  = _w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise RuntimeError(
            f"Transaction reverted on-chain (tx={tx_hash.hex()}). "
            "Likely cause: caller is not an authorized issuer, or a contract "
            "require() failed. Inspect the Hardhat node logs for the revert reason."
        )
    return receipt


# ---------------------------------------------------------------------------
# DID operations
# ---------------------------------------------------------------------------

async def create_did(did: str, public_key: str, service_endpoint: str) -> dict:
    _ensure_initialised()
    _send_tx(_did_registry.functions.createDID(did, public_key, service_endpoint))
    return await resolve_did(did)


async def resolve_did(did: str) -> dict | None:
    _ensure_initialised()
    try:
        doc = _did_registry.functions.resolveDID(did).call()
        return {
            "controller":       doc[0],
            "public_key":       doc[1],
            "service_endpoint": doc[2],
            "created":          doc[3],
            "updated":          doc[4],
            "active":           doc[5],
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Verifiable Credential operations
# ---------------------------------------------------------------------------

async def issue_credential(
    credential_id: bytes,
    vc_json: str,
    holder_did: str,
    vc_type: str,
    expires_at: int,
) -> None:
    _ensure_initialised()
    credential_hash = Web3.keccak(text=vc_json)
    _send_tx(
        _vc_registry.functions.issueCredential(
            credential_id, credential_hash, holder_did, vc_type, expires_at
        )
    )


async def verify_credential(credential_id_hex: str, credential_hash_hex: str) -> dict:
    _ensure_initialised()
    cred_id   = decode_credential_id(credential_id_hex)
    cred_hash = bytes.fromhex(credential_hash_hex.removeprefix("0x").zfill(64))

    valid, status_int = _vc_registry.functions.verifyCredential(cred_id, cred_hash).call()

    status_map = {0: "Active", 1: "Revoked", 2: "Suspended"}
    return {"valid": valid, "status": status_map.get(status_int, "Unknown")}


# ---------------------------------------------------------------------------
# Audit Trail operations
# ---------------------------------------------------------------------------

_EVENT_TYPE_MAP = {
    "OnboardingStarted":   0,
    "DocumentVerified":    1,
    "BiometricVerified":   2,
    "SanctionsCleared":    3,
    "RiskAssessed":        4,
    "CredentialIssued":    5,
    "OnboardingCompleted": 6,
    "OnboardingRejected":  7,
    "CredentialReused":    8,
}


async def record_audit_event(subject_did: str, event_type_name: str, payload: dict) -> None:
    event_type_int = _EVENT_TYPE_MAP.get(event_type_name)
    if event_type_int is None:
        # Event type not in the on-chain enum; skip blockchain recording.
        # Its outcome is captured implicitly in the subsequent RiskAssessed event.
        return

    _ensure_initialised()
    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    data_hash     = Web3.keccak(primitive=payload_bytes)

    _send_tx(
        _audit_trail.functions.recordEvent(
            data_hash,
            subject_did,
            event_type_int,
        )
    )
