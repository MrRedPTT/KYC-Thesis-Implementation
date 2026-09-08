// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title VCRegistry
 * @notice Manages the lifecycle (issuance, verification, revocation) of
 *         W3C Verifiable Credentials for the KYC framework.
 *
 *         Only addresses registered in the Trust Registry (authorizedIssuers)
 *         may issue or revoke credentials — mapping to "Artigo 25.º of Lei
 *         n.º 83/2017" (Third-Party Reliance / Recurso a Terceiros).
 *
 *         The contract stores only a keccak256 hash of the credential JSON;
 *         the raw VC is stored off-chain (IPFS / Secure Document Storage).
 */
contract VCRegistry {
    enum CredentialStatus { Active, Revoked, Suspended }

    struct CredentialRecord {
        bytes32          credentialHash;  // keccak256(VC JSON)
        address          issuer;
        string           holderDID;
        string           vcType;          // e.g. "KYCCredential"
        uint256          issuedAt;
        uint256          expiresAt;       // unix timestamp; 0 = no expiry
        CredentialStatus status;
    }

    // credentialId (32-byte application-level UUID) -> record
    mapping(bytes32 => CredentialRecord) private _credentials;

    // Trust Registry: banks licensed by Banco de Portugal
    mapping(address => bool) public authorizedIssuers;
    address public admin;

    event CredentialIssued(bytes32 indexed credentialId, address indexed issuer, string holderDID);
    event CredentialRevoked(bytes32 indexed credentialId, address indexed revokedBy);
    event CredentialSuspended(bytes32 indexed credentialId);
    event IssuerAuthorized(address indexed issuer);
    event IssuerRevoked(address indexed issuer);

    modifier onlyAdmin() {
        require(msg.sender == admin, "VCRegistry: only admin");
        _;
    }

    modifier onlyAuthorizedIssuer() {
        require(authorizedIssuers[msg.sender], "VCRegistry: not an authorized issuer");
        _;
    }

    constructor() {
        admin = msg.sender;
    }

    // -----------------------------------------------------------------------
    // Trust Registry management (admin only)
    // -----------------------------------------------------------------------

    function authorizeIssuer(address issuer) external onlyAdmin {
        authorizedIssuers[issuer] = true;
        emit IssuerAuthorized(issuer);
    }

    function revokeIssuerAuthorization(address issuer) external onlyAdmin {
        authorizedIssuers[issuer] = false;
        emit IssuerRevoked(issuer);
    }

    // -----------------------------------------------------------------------
    // Credential lifecycle (authorized issuers only)
    // -----------------------------------------------------------------------

    /**
     * @param credentialId  Application-level UUID as bytes32.
     * @param credentialHash keccak256 of the canonical VC JSON.
     * @param holderDID     W3C DID of the credential subject.
     * @param vcType        Human-readable credential type string.
     * @param expiresAt     Unix timestamp or 0 for non-expiring.
     */
    function issueCredential(
        bytes32 credentialId,
        bytes32 credentialHash,
        string  calldata holderDID,
        string  calldata vcType,
        uint256 expiresAt
    ) external onlyAuthorizedIssuer {
        require(_credentials[credentialId].issuedAt == 0, "VCRegistry: credential ID already exists");

        _credentials[credentialId] = CredentialRecord({
            credentialHash: credentialHash,
            issuer:         msg.sender,
            holderDID:      holderDID,
            vcType:         vcType,
            issuedAt:       block.timestamp,
            expiresAt:      expiresAt,
            status:         CredentialStatus.Active
        });

        emit CredentialIssued(credentialId, msg.sender, holderDID);
    }

    function revokeCredential(bytes32 credentialId) external onlyAuthorizedIssuer {
        require(_credentials[credentialId].issuer == msg.sender, "VCRegistry: only original issuer can revoke");
        _credentials[credentialId].status = CredentialStatus.Revoked;
        emit CredentialRevoked(credentialId, msg.sender);
    }

    function suspendCredential(bytes32 credentialId) external onlyAuthorizedIssuer {
        require(_credentials[credentialId].issuer == msg.sender, "VCRegistry: only original issuer can suspend");
        _credentials[credentialId].status = CredentialStatus.Suspended;
        emit CredentialSuspended(credentialId);
    }

    // -----------------------------------------------------------------------
    // Verification (public — callable by any Verifier / Bank B)
    // -----------------------------------------------------------------------

    /**
     * @return valid   True if hash matches, credential is Active, and not expired.
     * @return status  Current CredentialStatus enum value.
     */
    function verifyCredential(
        bytes32 credentialId,
        bytes32 credentialHash
    ) external view returns (bool valid, CredentialStatus status) {
        CredentialRecord memory r = _credentials[credentialId];
        require(r.issuedAt > 0, "VCRegistry: credential not found");

        bool hashMatch  = r.credentialHash == credentialHash;
        bool notExpired = r.expiresAt == 0 || block.timestamp <= r.expiresAt;
        bool isActive   = r.status == CredentialStatus.Active;

        return (hashMatch && notExpired && isActive, r.status);
    }

    function getCredential(bytes32 credentialId) external view returns (CredentialRecord memory) {
        require(_credentials[credentialId].issuedAt > 0, "VCRegistry: not found");
        return _credentials[credentialId];
    }
}
