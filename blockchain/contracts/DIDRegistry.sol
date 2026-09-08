// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title DIDRegistry
 * @notice Stores W3C-compliant DID Documents for the KYC framework.
 *         Each user/bank receives a Decentralized Identifier (DID) whose
 *         Document (public key + service endpoint) is anchored on-chain.
 */
contract DIDRegistry {
    struct DIDDocument {
        address controller;       // Ethereum address that controls this DID
        string  publicKey;        // JSON-encoded JWK or multibase public key
        string  serviceEndpoint;  // URL for the identity service
        uint256 created;
        uint256 updated;
        bool    active;
    }

    mapping(string => DIDDocument) private _documents;

    event DIDCreated(string indexed did, address indexed controller);
    event DIDUpdated(string indexed did);
    event DIDDeactivated(string indexed did);

    modifier onlyController(string calldata did) {
        require(_documents[did].controller == msg.sender, "DIDRegistry: not controller");
        _;
    }

    // -----------------------------------------------------------------------
    // Write
    // -----------------------------------------------------------------------

    function createDID(
        string calldata did,
        string calldata publicKey,
        string calldata serviceEndpoint
    ) external {
        require(bytes(_documents[did].publicKey).length == 0, "DIDRegistry: DID already exists");

        _documents[did] = DIDDocument({
            controller:      msg.sender,
            publicKey:       publicKey,
            serviceEndpoint: serviceEndpoint,
            created:         block.timestamp,
            updated:         block.timestamp,
            active:          true
        });

        emit DIDCreated(did, msg.sender);
    }

    function updateDID(
        string calldata did,
        string calldata publicKey,
        string calldata serviceEndpoint
    ) external onlyController(did) {
        require(_documents[did].active, "DIDRegistry: DID is deactivated");

        _documents[did].publicKey       = publicKey;
        _documents[did].serviceEndpoint = serviceEndpoint;
        _documents[did].updated         = block.timestamp;

        emit DIDUpdated(did);
    }

    function deactivateDID(string calldata did) external onlyController(did) {
        _documents[did].active = false;
        emit DIDDeactivated(did);
    }

    // -----------------------------------------------------------------------
    // Read
    // -----------------------------------------------------------------------

    function resolveDID(string calldata did) external view returns (DIDDocument memory) {
        require(bytes(_documents[did].publicKey).length > 0, "DIDRegistry: DID not found");
        return _documents[did];
    }

    function exists(string calldata did) external view returns (bool) {
        return bytes(_documents[did].publicKey).length > 0;
    }
}
