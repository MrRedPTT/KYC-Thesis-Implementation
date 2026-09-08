// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title AuditTrail
 * @notice Tamper-proof, append-only log of onboarding events.
 *         Stores only the keccak256 hash of the event payload;
 *         raw event data lives off-chain (Operational Database).
 *
 *         Satisfies the audit requirements of:
 *           - Artigo 51.º of Lei n.º 83/2017 (7-year retention)
 *           - AMLR audit trail obligations
 *           - EBA KYC record-keeping guidelines
 */
contract AuditTrail {
    enum EventType {
        OnboardingStarted,    // 0
        DocumentVerified,     // 1
        BiometricVerified,    // 2
        SanctionsCleared,     // 3
        RiskAssessed,         // 4
        CredentialIssued,     // 5
        OnboardingCompleted,  // 6
        OnboardingRejected,   // 7
        CredentialReused      // 8
    }

    struct AuditEvent {
        bytes32   dataHash;     // keccak256 of off-chain JSON payload
        string    subjectDID;   // DID of the onboarding subject
        address   recordedBy;   // backend service address
        EventType eventType;
        uint256   timestamp;
    }

    AuditEvent[] private _log;

    // DID -> indices into _log (supports per-subject history retrieval)
    mapping(string => uint256[]) private _didIndex;

    event AuditEventRecorded(
        uint256   indexed eventIndex,
        string    indexed subjectDID,
        EventType         eventType
    );

    // -----------------------------------------------------------------------
    // Write (any authorized backend service may call)
    // -----------------------------------------------------------------------

    /**
     * @param dataHash    keccak256 of the canonical JSON event payload.
     * @param subjectDID  W3C DID of the onboarding subject.
     * @param eventType   One of the EventType enum values.
     * @return eventIndex Index of the newly appended event.
     */
    function recordEvent(
        bytes32   dataHash,
        string    calldata subjectDID,
        EventType eventType
    ) external returns (uint256 eventIndex) {
        eventIndex = _log.length;

        _log.push(AuditEvent({
            dataHash:   dataHash,
            subjectDID: subjectDID,
            recordedBy: msg.sender,
            eventType:  eventType,
            timestamp:  block.timestamp
        }));

        _didIndex[subjectDID].push(eventIndex);

        emit AuditEventRecorded(eventIndex, subjectDID, eventType);
    }

    // -----------------------------------------------------------------------
    // Read
    // -----------------------------------------------------------------------

    function getEvent(uint256 index) external view returns (AuditEvent memory) {
        require(index < _log.length, "AuditTrail: index out of bounds");
        return _log[index];
    }

    function getEventsByDID(string calldata subjectDID) external view returns (uint256[] memory) {
        return _didIndex[subjectDID];
    }

    function totalEvents() external view returns (uint256) {
        return _log.length;
    }
}
