# Architecture & Design Decisions

This document explains how the prototype works end-to-end and why specific
design choices were made. It is the companion to [README.md](README.md) (which
covers *how to run* the system). Read this when you want to understand *what
the system actually does*, *what attacks it prevents*, and *what it deliberately
does not claim to solve*.

---

## 1. What this prototype is

A four-step remote KYC ("Know Your Customer") onboarding flow:

1. **Initiation** — the holder declares who they are and supplies an Ethereum
   address that will own their identity.
2. **Document verification** — an identity document is OCR'd, its MRZ
   (Machine-Readable Zone) is parsed and the ICAO 9303 check digits are
   validated, and the portrait is cropped out for biometric comparison.
3. **Biometric verification** — a short selfie video is matched 1:1 against
   the document portrait (ArcFace) and run through a passive liveness check
   (rPPG-POS) to detect deepfakes and replays.
4. **Completion** — the verification outcomes are scored, and if approved a
   W3C Verifiable Credential is issued. Only its hash and lifecycle state
   live on the blockchain; the raw credential stays off-chain.

Every meaningful event in steps 1–4 is anchored as a tamper-evident hash in an
on-chain audit log (`AuditTrail.sol`).

---

## 2. Component map

```
                         ┌──────────────────────────────────────────────┐
                         │                  Client                      │
                         │ (Swagger UI / curl / future EUDI Wallet app) │
                         └──────────────────────────────────────────────┘
                                              │  HTTPS
                                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       FastAPI backend (backend/app/)                     │
│                                                                          │
│   routers/onboarding.py   ─── 4-step REST flow (+ challenge), session    │
│   routers/verification.py ─── /verification/credential (Bank-B reuse)    │
│   routers/identity.py     ─── DID Document CRUD                          │
│                                                                          │
│   services/ai_service.py        ── glue → ai_verification/*              │
│   services/blockchain_service.py ── web3.py → Solidity contracts         │
│                                                                          │
│   utils/credential_id.py        ── shared bytes32 encode/decode          │
└──────────────────────────────────────────────────────────────────────────┘
            │                                          │
            ▼                                          ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────────┐
│      ai_verification/           │   │      Hardhat / Solidity 0.8.20       │
│                                 │   │                                      │
│  document_auth.py               │   │  DIDRegistry.sol                     │
│   • EasyOCR + ICAO 9303 MRZ     │   │   • W3C DID Documents                │
│   • InsightFace SCRFD portrait  │   │   • onlyController updates           │
│  face_verification.py           │   │                                      │
│   • ArcFace 512-d embeddings    │   │  VCRegistry.sol                      │
│   • cosine distance ≤ 0.40      │   │   • keccak256(VC) + status enum      │
│  liveness.py                    │   │   • Trust Registry of issuers        │
│   • rPPG-POS, peakiness > 1.70  │   │                                      │
│  session_quality.py             │   │  AuditTrail.sol                      │
│   • fps/res/duration/luminance  │   │   • Append-only, EventType enum      │
│  temporal_consistency.py        │   │                                      │
│   • inter-frame discontinuity   │   │                                      │
│  audio_consistency.py           │   │                                      │
│   • flatness, energy, lip-sync  │   │                                      │
│  risk_scoring.py                │   │                                      │
│   • Rule-based heuristic        │   │                                      │
└─────────────────────────────────┘   └──────────────────────────────────────┘
```

---

## 3. End-to-end request flow

### Step 1 — `POST /api/v1/onboarding/start`

Input: `{full_name, nationality, document_type, holder_address}` where
`holder_address` is a checksum Ethereum address.

What happens:

1. `_derive_holder_did()` validates the address with `Web3.is_address` and
   produces `did:ethr:<checksum-address>`. **This DID is the audit-trail
   subject for every subsequent event in this session.**
2. A UUID4 `session_id` is generated and the session is held in an in-memory
   dict (`_sessions`).
3. An `OnboardingStarted` event is appended to `AuditTrail.sol`, with
   `keccak256({session_id, nationality})` as the payload hash.

Returned: `{session_id, holder_did, status}`.

### Step 2 — `POST /api/v1/onboarding/{session_id}/document`

Input: multipart JPEG/PNG of the document.

What happens (`ai_verification/document_auth.py`):

1. Decode the image (OpenCV → Pillow fallback for WebP).
2. Upscale to at least 1200 px on the longer edge for OCR accuracy.
3. EasyOCR extracts all text blocks; mean confidence becomes a quality
   signal (< 0.50 raises a `low_ocr_confidence` flag).
4. Extract MRZ-shaped lines (2 × ≥ 30 chars, alphanumeric + `<`), parse the
   ICAO 9303 **TD3** format, compute Annex-B check digits for the document
   number and expiry, and reject if either fails.
5. Reject if the expiry date is in the past.
6. Crop the portrait using InsightFace SCRFD; if SCRFD is unavailable, fall
   back to OpenCV Haar cascade. The crop is returned as JPEG bytes for
   step 3.
7. A `DocumentVerified` event is appended to `AuditTrail.sol` with the
   `{authentic, confidence}` payload hash.

The portrait is **stored only in the in-memory session** for step 3 — it is
never written to disk or to the chain.

### Step 2b — `GET` / `POST /api/v1/onboarding/{session_id}/challenge`

A knowledge-based contextual challenge, generated at document upload from MRZ
fields while they are still in session state (`_build_challenge()`).

What happens:

1. `GET` returns the question, or `available: false` when the MRZ yielded too
   little data to form one. There is no penalty in that case.
2. `POST` compares the submitted answer against the expected value and stores
   `challenge_pass` on the session.
3. `risk_scoring.py` reads `challenge_pass` at Step 4. A wrong answer raises
   the score; an unanswered or unavailable challenge is neutral.

The purpose is to bind the person driving the session to the document they
uploaded. An attacker holding a stolen document image and a synthetic video of
its holder still has to answer a question about the document's contents, which
an injection attack alone does not supply.

### Step 3 — `POST /api/v1/onboarding/{session_id}/biometric`

Input: multipart MP4/WebM video ≥ ~4 s at 30 fps with a visible frontal face.

What happens:

1. `_extract_middle_frame()` pulls a single still image from the video for
   the 1:1 face match.
2. `face_verification.py` runs both images through InsightFace `buffalo_l`
   (ArcFace 512-d embeddings), computes cosine distance, and matches if it
   is `≤ 0.40`.
3. `liveness.py` runs **every** frame through Haar cascade face detection,
   resizes each face crop to 128×128, normalises to `[0, 1]`, and feeds the
   stack to `POS_WANG` from rPPG-Toolbox. The output BVP signal is FFT-ed
   in the 45–150 BPM band and **peakiness** (peak / mean magnitude in band)
   is the liveness score. A real face produces a sharp cardiac peak; a
   deepfake or a still photo does not. The decision threshold is 1.70.
4. Three further gates run over the same clip, each returning a `pass` flag
   and a list of `flags` naming whichever specific check failed:
   - `session_quality.py` — frame rate ≥ 24 fps, resolution ≥ 640×480,
     duration 5–60 s, duplicate frames ≤ 10 %, mean luminance 30–220, and
     reported vs. actual frame count within 20 %. Below these bounds the rPPG
     signal is not reliable enough for the liveness verdict to mean anything.
   - `temporal_consistency.py` — inter-frame pixel differences, catching cuts,
     stitched loops, and frozen or replayed segments.
   - `audio_consistency.py` — audio-track presence, silence ratio, spectral
     flatness (TTS output is measurably flatter than natural speech), energy
     variance, and Pearson correlation between the audio RMS envelope and
     mouth-region pixel motion as a lip-sync check.
5. A `BiometricVerified` event is appended to `AuditTrail.sol`.

Each gate is independent by design. Chapter 5 of the thesis found that no single
detector held up across generation pipelines, so a verdict resting on one signal
is one bypass away from useless.

### Step 4 — `POST /api/v1/onboarding/{session_id}/complete`

No parameters — the holder DID was fixed at `/start`.

What happens:

1. `risk_scoring.py` aggregates the session into a `{low, medium, high}`
   risk level. Currently rule-based (see §8 — Limitations).
2. A `RiskAssessed` event is appended to `AuditTrail`.
3. If risk is **low or medium**:
   - A canonical W3C VC JSON is built (`sort_keys=True` so the hash is
     reproducible).
   - `make_credential_id()` produces a `bytes32` identifier (UUID4 padded
     left into 32 bytes) and the matching `0x`-prefixed 66-char hex
     representation.
   - `VCRegistry.issueCredential` stores `keccak256(VC JSON)`, the holder
     DID, the type, and an `expiresAt` 3 years from issuance. The
     `onlyAuthorizedIssuer` modifier enforces that only Trust-Registry
     addresses can do this.
   - A `CredentialIssued` event is appended to `AuditTrail`.
4. If risk is **high**, no VC is issued; an `OnboardingRejected` event is
   appended.
5. The in-memory session is dropped.

Returned: `{session_id, risk_level, approved, credential_id?, detail}`.

### Cross-institutional reuse — `POST /api/v1/verification/credential`

A second institution ("Bank B") can verify a credential **without
contacting the issuer**:

1. The holder presents the `credential_id` and recomputes the
   `credential_hash` from the off-chain VC JSON they hold.
2. Bank B's backend calls `VCRegistry.verifyCredential(id, hash)`.
3. The contract returns `(valid, status)` where `valid = hashMatches &&
   notExpired && status == Active`.

If the off-chain VC was tampered with after issuance, the recomputed hash
will not match the anchored one and `valid` will be false. **No PII is
transmitted between the two institutions.**

---

## 4. Trust model

Who must be trusted for what:

| Party | Trusted to | Not trusted with |
|---|---|---|
| `VCRegistry.admin` | Maintain the Trust Registry (`authorizeIssuer` / `revokeIssuerAuthorization`) | Issue, revoke, or modify individual credentials |
| Authorized issuer ("Bank A") | Perform the initial KYC honestly; issue and revoke VCs they originate | Modify another issuer's credentials |
| Holder (data subject) | Custody their own private key and VC JSON | Modify their own VC after issuance (hash won't match) |
| Verifier ("Bank B") | Read on-chain state | Anything else — they receive no PII |
| Backend service | Hold the issuer's signing key, anchor audit events | Modify the audit log (it's append-only by contract) |

The architecture deliberately makes Bank A **incapable** of silently
falsifying its own audit trail after the fact — see §5.2.

---

## 5. Issues this design prevents (and how)

### 5.1 Centralized biometric "honeypot"

**The risk:** Traditional KYC systems store millions of high-resolution face
images and full ID scans in one place. A single breach (e.g., the 2024 AMA
incident in Portugal) hands all of it to an attacker.

**What we do:** Biometric templates and raw ID images are **never written to
the blockchain**. The document portrait lives in the in-memory session for
the duration of one onboarding flow (minutes), and the original document is
stored off-chain encrypted at the bank. The on-chain VC is just a
`keccak256` hash — useless to an attacker who breaches it.

**Where in code:** [blockchain/contracts/VCRegistry.sol:81-101](blockchain/contracts/VCRegistry.sol#L81-L101)
stores only `credentialHash` (`bytes32`); the raw VC JSON is built and
hashed in [backend/app/routers/onboarding.py](backend/app/routers/onboarding.py) but never written
to disk or chain.

### 5.2 Audit log tampering by the bank itself

**The risk:** In a traditional system, the bank both produces the audit log
*and* controls the database. Under regulatory scrutiny it could in
principle delete or backdate records and no one would know.

**What we do:** `AuditTrail.sol` exposes only `recordEvent` (append). There
is no `update`, no `delete`, no admin escape hatch. Each entry's payload
hash and timestamp are committed at block-confirmation time. Even the
contract deployer cannot rewrite history.

**Where in code:** [blockchain/contracts/AuditTrail.sol:57-75](blockchain/contracts/AuditTrail.sol#L57-L75)
— the only state-mutating function appends to a growing array.

### 5.3 Cross-institutional data sharing without bilateral agreements

**The risk:** "Article 25.º Third-Party Reliance" in Portuguese law lets
Bank B rely on Bank A's KYC, but in practice every pair of banks needs a
contractual data-sharing agreement and a custom integration. This is why
KYC reuse barely happens.

**What we do:** The Trust Registry on `VCRegistry` is **global**. Bank B
checks one piece of public state — *is Bank A's address in
`authorizedIssuers`?* — and one contract call — *does
`verifyCredential(id, hash)` return true?* No bilateral integration, no
PII transmission, no real-time round-trip to Bank A.

**Where in code:** [blockchain/contracts/VCRegistry.sol:32-50](blockchain/contracts/VCRegistry.sol#L32-L50)
defines the Trust Registry and the `onlyAuthorizedIssuer` modifier;
[routers/verification.py](backend/app/routers/verification.py) demonstrates the
reuse-side call.

### 5.4 Deepfake injection attacks

**The risk:** Modern diffusion models (Hedra, Google Veo 3, Kling AI) can
produce face videos that defeat texture-based liveness detectors entirely.
In the thesis evaluation, MiniFASNet flagged **zero frames** on Hedra and
Veo 3 outputs.

**What we do:** Passive rPPG-POS extracts the cardiac BVP signal from
subtle skin colour changes over time and measures spectral peakiness in
the 45–150 BPM band. Real faces have a sharp peak (peakiness > 1.70);
synthetic faces produce no consistent pulse. In the thesis experiments,
both Hedra and Veo 3 sat firmly below threshold (1.23 and 1.59).

**Where in code:** [ai_verification/liveness.py:136-162](ai_verification/liveness.py#L136-L162)
implements the peakiness computation.

**Limitation:** rPPG requires a video of ≥ ~4 s at 30 fps with a
reasonably stable frontal face. A single still image cannot be classified
and the endpoint returns `is_live: false`.

### 5.5 Document tampering after issuance

**The risk:** The off-chain VC could be altered between issuance and reuse.

**What we do:** `verifyCredential(id, hash)` returns `valid = true` only if
the supplied hash matches the one anchored at issuance. Any byte change to
the VC JSON shifts the `keccak256` output and the verifier rejects it.

**Where in code:** [blockchain/contracts/VCRegistry.sol:118-135](blockchain/contracts/VCRegistry.sol#L118-L135).

### 5.6 Unauthorized credential issuance

**The risk:** A compromised or rogue address pretending to be a bank could
issue VCs that downstream verifiers would trust.

**What we do:** `onlyAuthorizedIssuer` gates `issueCredential`,
`revokeCredential`, and `suspendCredential`. Only addresses explicitly
added to `authorizedIssuers` by the admin (representing the regulator in
the production model) can mutate the credential store. Authorisation is
revocable per-address.

### 5.7 Silent on-chain transaction reverts

**The risk:** Earlier, `_send_tx` waited for the receipt but did not check
its status. A reverted transaction (e.g., the backend's address was not in
`authorizedIssuers`) returned silently and the caller thought the
credential had been issued. The user would receive a fake "approved"
response.

**What we do:** [backend/app/services/blockchain_service.py](backend/app/services/blockchain_service.py)
now raises `RuntimeError` whenever `receipt.status != 1`, with a hint
pointing at the most likely cause.

### 5.8 Encoding drift between issuer and verifier

**The risk:** A `bytes32` credential identifier has to round-trip from the
issuer's response back to the verifier's contract call. If the two sides
disagree on how to serialise/deserialise that 32-byte value, every
`verifyCredential` call returns "credential not found" — silently. This
was a real bug in the original implementation: issuance padded the UUID
**left**-aligned, the verifier reconstructed it **right**-aligned, and
the lookup always missed.

**What we do:** Both sides now go through a single helper module,
[backend/app/utils/credential_id.py](backend/app/utils/credential_id.py).
Encoding and decoding cannot drift apart because they share one source of
truth. A pytest suite at
[backend/tests/test_credential_roundtrip.py](backend/tests/test_credential_roundtrip.py)
pins the contract:

```
tests/test_credential_roundtrip.py::test_roundtrip_recovers_stored_bytes PASSED
tests/test_credential_roundtrip.py::test_returned_hex_is_full_bytes32 PASSED
tests/test_credential_roundtrip.py::test_decoded_value_is_32_bytes PASSED
tests/test_credential_roundtrip.py::test_uuid_is_left_aligned_within_bytes32 PASSED
tests/test_credential_roundtrip.py::test_decoder_accepts_short_hex_via_zfill PASSED
```

These run without Hardhat, without `.env`, without any model weights — so
they can sit in CI and catch regressions cheaply.

### 5.9 Backend crash on import / hidden configuration errors

**The risk:** The original `blockchain_service.py` called
`Web3.to_checksum_address("0x0")` and `account.from_key("")` at module
import time. Any code that imported it without a fully populated `.env`
(unit tests, doc generation, `python -c "import app.main"`) crashed with
opaque cryptography errors during import — before FastAPI even started.

**What we do:** All web3 objects are now lazy-initialised in
`_ensure_initialised()`, called on the first on-chain operation. Missing
environment variables raise a *named* `RuntimeError` pointing the
developer at `scripts/deploy.js`. Importing the module without
configuration now succeeds.

### 5.10 Placeholder DIDs masquerading as real ones

**The risk:** Earlier, every audit event in steps 1–3 was anchored under a
`did:pending:<session-uuid>` placeholder. This wasn't a real W3C DID
method, so audit log entries weren't actually queryable by a real holder
DID. It also wasn't disclosed clearly to a verifier reading the log.

**What we do:** The holder Ethereum address is taken at `/start`, the
backend derives `did:ethr:<checksum-address>` via `Web3.to_checksum_address`,
and **every** subsequent event uses this real DID as the subject. The
placeholder is gone from the entire flow.

**Where in code:** [backend/app/routers/onboarding.py](backend/app/routers/onboarding.py)
`_derive_holder_did()`.

---

## 6. On-chain vs off-chain split

This table is the same one in the thesis chapter (Table "On-chain vs Off-chain
Data Partitioning"), reproduced here for quick reference while reading code.

| Data / Operation | Location | Why |
|---|---|---|
| DID public key + service endpoint | On-chain (DIDRegistry) | Must be publicly resolvable; no PII |
| `keccak256(VC JSON)` | On-chain (VCRegistry) | Immutable binding; reversing the hash to PII is computationally infeasible |
| Credential status (Active / Revoked / Suspended) | On-chain (VCRegistry) | Real-time global revocation |
| `keccak256(event payload)` | On-chain (AuditTrail) | Tamper-evidence for AMLR audits |
| Raw VC JSON | Off-chain (encrypted DB / IPFS) | Contains PII; subject to GDPR rectification & erasure |
| ID documents + biometric video | Off-chain (Secure Document Storage) | High-sensitivity PII; 7-year retention under Art. 51.º Lei 83/2017 |
| Event payload JSON | Off-chain (Operational DB) | Linked to on-chain hash; purged after legal retention period |

**Why never PII on chain:** a blockchain entry is forever and globally
visible. Article 17 GDPR ("right to erasure") is incompatible with that.
Anchoring only the hash gives tamper-evidence without making the underlying
PII permanent — when the bank purges the off-chain copy, the hash remains
but cannot be reversed.

---

## 7. Data lifecycle

```
   issued                         end of business relationship
   ────►                                           ────►
   │                                                  │
   │            7 years (Art. 51.º Lei 83/2017)       │
   ▼                                                  ▼
[off-chain   ─────── encrypted, read-only after termination ───────►  PURGE
 raw VC,
 docs,                                                                ▲
 video]                                                               │
                                                                      │
[on-chain    ─────── immutable, no PII, retained indefinitely ────────┘
 hash +
 status +
 audit
 events]
```

The off-chain side is purgeable; the on-chain side is permanent but contains
no recoverable personal data after the off-chain copy is destroyed.

---

## 8. What this design does NOT solve (be honest in defence)

These are deliberately out of scope for the prototype and are listed as
limitations in the thesis chapter. Do not let a reviewer push you into
claiming otherwise.

| Gap | Status | Why it isn't here |
|---|---|---|
| Sanctions / PEP screening | `[Designed]` only | The `SanctionsCleared` EventType is reserved but no module integrates EU / UN / OFAC lists |
| Gradient-Boosted-Trees risk model | Rule-based heuristic implemented | No labelled training data was available; the rule-based scorer produces the same `low`/`medium`/`high` output schema |
| EUDI Wallet integration | Future work | Pending production wallet reference implementations |
| Cross-institutional pilot | Same chain, one node | The reuse code path is exercised, but no second-institution deployment exists |
| EDD-referral branch | Binary approve/reject | `risk_level == high` is rejected; the architectural three-way split (approve / refer for EDD / decline) is not implemented |
| DID Document registration in the onboarding flow | Manual | The `did:ethr` identifier is derived automatically, but publishing the DID Document (public key + service endpoint) to `DIDRegistry` is a separate `/api/v1/identity/did` call |
| Production gas / latency benchmarks | Not measured | All tests run against an instant-mining Hardhat node |

---

## 9. Where to look next

| If you want to... | Read |
|---|---|
| Run the system | [README.md](README.md) |
| Understand the contracts | [blockchain/contracts/](blockchain/contracts/) — all three files are < 150 lines and heavily commented |
| Read the API surface | <http://localhost:8000/docs> (Swagger UI) once the backend is running |
| Cite this in the thesis | The "Proposed Architecture" section of `Chapters/AIOnboardingAndBlockchain.tex` |
| Confirm the encoding fix | `cd backend && python -m pytest tests/ -v` |
