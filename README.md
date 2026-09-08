# KYC Blockchain Prototype

Proof-of-concept for AI-powered remote KYC onboarding with blockchain-anchored Verifiable Credentials, developed as part of a Master's thesis on secure digital identity for the Portuguese banking sector.

## Architecture

```
KYC-Thesis-Implementation/
├── blockchain/          Hardhat project — 3 Solidity contracts
│   ├── contracts/
│   │   ├── DIDRegistry.sol     W3C DID anchoring
│   │   ├── VCRegistry.sol      VC lifecycle + on-chain Trust Registry
│   │   └── AuditTrail.sol      Append-only compliance audit log
│   └── scripts/deploy.js       Deploys all 3 contracts + registers issuer
├── backend/             FastAPI application (Python 3.11)
│   └── app/
│       ├── routers/            REST endpoints (/onboarding, /verification, /identity)
│       ├── services/
│       │   ├── ai_service.py   Bridges FastAPI ↔ ai_verification modules
│       │   └── blockchain_service.py  web3.py ↔ Hardhat contracts
│       └── models/schemas.py   Pydantic request/response models
├── ai_verification/     Off-chain verification pipeline (Python)
│   ├── document_auth.py        EasyOCR + ICAO 9303 MRZ validation + portrait crop
│   ├── face_verification.py    InsightFace ArcFace 1:1 matching (cosine dist ≤ 0.40)
│   ├── liveness.py             rPPG-POS frequency-domain liveness (peakiness ≥ 1.70)
│   ├── session_quality.py      Capture-quality gate (fps, resolution, duration, luminance)
│   ├── temporal_consistency.py Inter-frame continuity (cuts, loops, frozen segments)
│   ├── audio_consistency.py    Silence ratio, spectral flatness, lip-audio sync
│   └── risk_scoring.py         Rule-based risk score → low / medium / high
├── samples/              Document image + video the runners read (empty by design)
├── test_onboarding.ps1   End-to-end runner (PowerShell)
├── kyc_test.py           End-to-end runner (Python)
├── .env.example          Environment variable template
└── docker-compose.yml    Spins up Hardhat node + FastAPI together
```

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Node.js | ≥ 20 | For Hardhat |
| npm | ≥ 10 | Bundled with Node 20 |
| Python | ≥ 3.11 | For the backend and AI modules |
| Docker + Compose | any recent | Optional — only needed for the Docker path |

> **Windows note:** all commands below are PowerShell. On Linux/macOS replace
> `Scripts\Activate.ps1` with `bin/activate` and use forward slashes.

---

## Option A — Manual setup (recommended for development)

### 1. Clone / open the project

The implementation lives at `d:\Desktop\Tese\implementation\` (or wherever you placed it).

### 2. Start the local Ethereum node

```powershell
cd implementation\blockchain
npm install --force    # --force needed to resolve hardhat-toolbox peer deps
npx hardhat node       # keep this terminal open — JSON-RPC on http://127.0.0.1:8545
```

The node prints 20 pre-funded test accounts. Account 0 is the deployer used in `.env.example`.

### 3. Deploy the contracts (new terminal)

```powershell
cd implementation\blockchain
npx hardhat run scripts/deploy.js --network localhost
```

Copy the three addresses printed at the end:

```
DIDRegistry deployed to: 0x...
VCRegistry  deployed to: 0x...
AuditTrail  deployed to: 0x...
```

### 4. Create the backend `.env`

```powershell
Copy-Item .env.example backend\.env
```

Open `backend\.env` and fill in the three addresses. The private key and RPC URL are pre-filled for local Hardhat:

```env
DEPLOYER_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
DID_REGISTRY_ADDRESS=<paste from step 3>
VC_REGISTRY_ADDRESS=<paste from step 3>
AUDIT_TRAIL_ADDRESS=<paste from step 3>
BLOCKCHAIN_RPC_URL=http://127.0.0.1:8545
```

> **Security:** the private key above is the well-known Hardhat test account. Never use it with real funds or on a public network.

### 5. Clone rPPG-Toolbox

The liveness check uses the POS implementation from **rPPG-Toolbox**, which is
not on PyPI and therefore has to be cloned rather than pip-installed:

```bash
git clone https://github.com/ubicomplab/rPPG-Toolbox.git Tools/rPPG-Toolbox
```

`liveness.py` looks for it in this order, all resolved relative to the module
so they hold regardless of the working directory:

1. `$RPPG_TOOLBOX_PATH`, if set
2. `<repo>/Tools/rPPG-Toolbox`  (the command above)
3. `<repo>/../Tools/rPPG-Toolbox`
4. `<repo>/../../Tools/rPPG-Toolbox`

`Tools/` is git-ignored. If the toolbox lives elsewhere, point
`RPPG_TOOLBOX_PATH` at it instead of moving it. Without it, every other stage
still runs; only the liveness call raises.

### 6. Set up the Python environment

```powershell
cd implementation
python -m venv .venv
.venv\Scripts\Activate.ps1

# AI verification dependencies (EasyOCR, InsightFace, OpenCV, rPPG)
pip install -r ai_verification\requirements.txt

# Backend dependencies (FastAPI, web3.py, Pydantic, Uvicorn)
pip install -r backend\requirements.txt
```

> The InsightFace `buffalo_l` model pack (~300 MB) and the EasyOCR English
> model (~100 MB) are downloaded automatically on **first run** to
> `~/.insightface/models/` and `~/.EasyOCR/model/` respectively.

### 7. Start the backend

```powershell
cd implementation\backend
uvicorn app.main:app --reload --port 8000
```

The API is now live at **http://localhost:8000**.

---

## Option B — Docker Compose

```powershell
cd implementation

# Copy and edit the env file (fill in contract addresses after deploy)
Copy-Item .env.example backend\.env

# Build and start both services
docker compose up --build
```

The Hardhat node starts first. Run step 3 (deploy) once after containers are up, then restart only the backend:

```powershell
docker compose restart backend
```

---

## Running an end-to-end session

Two scripts drive the whole flow so you do not have to assemble the calls by
hand. Both print a colour-coded pass/fail line for every verification check:

```powershell
.\test_onboarding.ps1                       # PowerShell
```

```bash
python kyc_test.py                          # Python, same flow
```

Both default to `./samples/document.png` and `./samples/video.mp4`. That
directory is empty in this repository by design — see
[samples/README.md](samples/README.md) for what to put there and why the
original test corpus is not published.

Override the paths per run:

```powershell
.\test_onboarding.ps1 -DocPath '.\samples\id.png' -VideoPath '.\samples\clip.mp4' -ChallengeAnswer skip
```

```bash
python kyc_test.py --doc ./samples/id.png --video ./samples/clip.mp4 --challenge skip
```

For repeated local runs, copy the script rather than editing the committed one:

```bash
cp test_onboarding.ps1 test_onboarding.local.ps1
cp kyc_test.py         kyc_test.local.py
```

Both `*.local.*` names are git-ignored, so your own document paths and personal
details stay out of the repository.

---

## Using the API

Open **http://localhost:8000/docs** for the interactive Swagger UI.

The onboarding flow is four sequential steps, with an optional challenge
sub-step after the document upload:

| Step | Endpoint | Input | Notes |
|------|----------|-------|-------|
| 1 | `POST /api/v1/onboarding/start` | JSON body (name, nationality, doc type, holder Ethereum address) | Returns `session_id` and the derived `did:ethr` holder DID |
| 2 | `POST /api/v1/onboarding/{session_id}/document` | Multipart JPEG/PNG of ID document | OCR + MRZ validation + portrait crop. Also generates the contextual challenge while the MRZ is fresh |
| 2b | `GET /api/v1/onboarding/{session_id}/challenge` | — | Returns the knowledge-based question derived from the MRZ, or `available: false` if the MRZ yielded too little data |
| 2b | `POST /api/v1/onboarding/{session_id}/challenge` | JSON body (`answer`) | Records `challenge_pass`, which feeds the risk score. Skipping it does not penalise the score |
| 3 | `POST /api/v1/onboarding/{session_id}/biometric` | Multipart MP4/WebM ≥ 5 s, ≥ 640×480, ≥ 24 fps | ArcFace match, rPPG-POS liveness, plus the session-quality, temporal-consistency and audio-consistency gates |
| 4 | `POST /api/v1/onboarding/{session_id}/complete` | — | Risk score + VC issued on-chain if approved (holder DID is read from the session) |

The step-3 response carries a `pass` flag and a `flags` list for each of the
three extended checks alongside `face_match` and `liveness_pass`, so a rejection
can be attributed to a specific gate.

**Example with curl:**

```bash
# 1. Start session — holder_address is one of the Hardhat-funded accounts
SESSION=$(curl -s -X POST http://localhost:8000/api/v1/onboarding/start \
  -H "Content-Type: application/json" \
  -d '{
        "full_name":      "Maria Silva",
        "nationality":    "PT",
        "document_type":  "passport",
        "holder_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
      }' | jq -r .session_id)
echo "Session: $SESSION"

# 2. Upload document
curl -s -X POST "http://localhost:8000/api/v1/onboarding/$SESSION/document" \
  -F "file=@passport.jpg;type=image/jpeg" | jq .

# 2b. Contextual challenge (optional — skipping it does not penalise the score)
curl -s "http://localhost:8000/api/v1/onboarding/$SESSION/challenge" | jq .
curl -s -X POST "http://localhost:8000/api/v1/onboarding/$SESSION/challenge" \
  -H "Content-Type: application/json" \
  -d '{"answer":"PRT"}' | jq .

# 3. Upload biometric video (≥ 5 s, frontal face, ≥ 24 fps, with an audio track)
curl -s -X POST "http://localhost:8000/api/v1/onboarding/$SESSION/biometric" \
  -F "file=@selfie.mp4;type=video/mp4" | jq .

# 4. Complete — no parameters needed; holder DID was fixed at /start
curl -s -X POST "http://localhost:8000/api/v1/onboarding/$SESSION/complete" | jq .
```

The response from step 4 contains a `credential_id` (a `0x...` 66-char string). You can verify it independently:

```bash
# Recompute the canonical VC hash off-chain (must match the JSON used at issuance),
# then ask any participant to confirm the on-chain status:
curl -s -X POST http://localhost:8000/api/v1/verification/credential \
  -H "Content-Type: application/json" \
  -d '{"credential_id":"0x...","credential_hash":"0x..."}' | jq .
```

---

## Running the tests

The project ships with two test layers.

### Smart-contract tests (Hardhat / Mocha)

```powershell
cd implementation\blockchain
npx hardhat test
```

### Backend unit tests (pytest)

These cover the encoding contract that lets the verifier reconstruct a
credential's on-chain `bytes32` identifier from the hex string returned at
issuance — a regression test for a previously silent failure of the Bank-B
verification flow.

```powershell
cd implementation\backend
pip install pytest                # first time only
python -m pytest tests\ -v
```

Expected output:

```
tests/test_credential_roundtrip.py::test_roundtrip_recovers_stored_bytes PASSED
tests/test_credential_roundtrip.py::test_returned_hex_is_full_bytes32 PASSED
tests/test_credential_roundtrip.py::test_decoded_value_is_32_bytes PASSED
tests/test_credential_roundtrip.py::test_uuid_is_left_aligned_within_bytes32 PASSED
tests/test_credential_roundtrip.py::test_decoder_accepts_short_hex_via_zfill PASSED
============================== 5 passed ==============================
```

These tests have no infrastructure dependencies — they do not need Hardhat,
contract addresses, or a `.env` file. They can run in CI without any setup.

---

## Environment variables reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DEPLOYER_PRIVATE_KEY` | Yes | Ethereum private key used to sign transactions |
| `DID_REGISTRY_ADDRESS` | Yes | Address of deployed `DIDRegistry.sol` |
| `VC_REGISTRY_ADDRESS` | Yes | Address of deployed `VCRegistry.sol` |
| `AUDIT_TRAIL_ADDRESS` | Yes | Address of deployed `AuditTrail.sol` |
| `BLOCKCHAIN_RPC_URL` | Yes | JSON-RPC endpoint (default: `http://127.0.0.1:8545`) |
| `SEPOLIA_RPC_URL` | No | Infura/Alchemy URL for Sepolia testnet deployment |

---

## Project scope & limitations

This is a **thesis proof-of-concept**, not a production system. Specific constraints:

- The Hardhat node resets on restart — redeploy contracts and update `.env` addresses each time.
- Sessions are held in-memory; they are lost on backend restart.
- The holder DID is derived as `did:ethr:<checksum-address>` from the Ethereum address supplied at `/start`. Publishing the DID Document (public key + service endpoint) to `DIDRegistry` is a separate `/api/v1/identity/did` call and is not automatically orchestrated by the onboarding flow.
- Gas costs and confirmation latency on a production permissioned ledger (e.g., Hyperledger Besu) are not benchmarked.
- The liveness check requires a video of ≥ 30 frames (~4 s at 30 fps) with good frontal face visibility for the rPPG-POS algorithm to work correctly. Still images will be rejected.
