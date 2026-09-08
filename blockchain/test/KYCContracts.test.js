const hre = require("hardhat");
const { expect } = require("chai");

describe("KYC Contracts", function () {
    let didRegistry, vcRegistry, auditTrail;
    let deployer, bank, user;

    beforeEach(async () => {
        [deployer, bank, user] = await hre.ethers.getSigners();

        didRegistry = await hre.ethers.deployContract("DIDRegistry");
        vcRegistry  = await hre.ethers.deployContract("VCRegistry");
        auditTrail  = await hre.ethers.deployContract("AuditTrail");
    });

    // -------------------------------------------------------------------
    // DIDRegistry
    // -------------------------------------------------------------------
    describe("DIDRegistry", () => {
        it("creates and resolves a DID", async () => {
            const did = "did:ethr:0xABCDEF";
            await didRegistry.connect(user).createDID(did, '{"kty":"EC"}', "https://bank.example/svc");
            const doc = await didRegistry.resolveDID(did);
            expect(doc.controller).to.equal(user.address);
            expect(doc.active).to.be.true;
        });

        it("prevents duplicate DID registration", async () => {
            const did = "did:ethr:0xDUPE";
            await didRegistry.connect(user).createDID(did, "key", "svc");
            await expect(
                didRegistry.connect(user).createDID(did, "key2", "svc2")
            ).to.be.revertedWith("DIDRegistry: DID already exists");
        });

        it("only controller can update or deactivate", async () => {
            const did = "did:ethr:0xCTRL";
            await didRegistry.connect(user).createDID(did, "key", "svc");
            await expect(
                didRegistry.connect(bank).updateDID(did, "key2", "svc2")
            ).to.be.revertedWith("DIDRegistry: not controller");
        });
    });

    // -------------------------------------------------------------------
    // VCRegistry
    // -------------------------------------------------------------------
    describe("VCRegistry", () => {
        beforeEach(async () => {
            await vcRegistry.connect(deployer).authorizeIssuer(bank.address);
        });

        it("issues and verifies a credential", async () => {
            const credId   = hre.ethers.id("cred-001");
            const vcJson   = JSON.stringify({ subject: "did:ethr:0xUSER", type: "KYCCredential" });
            const credHash = hre.ethers.keccak256(hre.ethers.toUtf8Bytes(vcJson));

            const expiry = Math.floor(Date.now() / 1000) + 365 * 24 * 3600; // 1 year
            await vcRegistry.connect(bank).issueCredential(credId, credHash, "did:ethr:0xUSER", "KYCCredential", expiry);

            const [valid, status] = await vcRegistry.verifyCredential(credId, credHash);
            expect(valid).to.be.true;
            expect(status).to.equal(0); // Active
        });

        it("revocation invalidates a credential", async () => {
            const credId   = hre.ethers.id("cred-002");
            const credHash = hre.ethers.keccak256(hre.ethers.toUtf8Bytes("payload"));
            await vcRegistry.connect(bank).issueCredential(credId, credHash, "did:ethr:0xUSER", "KYCCredential", 0);

            await vcRegistry.connect(bank).revokeCredential(credId);

            const [valid, status] = await vcRegistry.verifyCredential(credId, credHash);
            expect(valid).to.be.false;
            expect(status).to.equal(1); // Revoked
        });

        it("rejects issuance from unauthorized addresses", async () => {
            await expect(
                vcRegistry.connect(user).issueCredential(
                    hre.ethers.id("cred-003"), hre.ethers.ZeroHash, "did:x", "KYC", 0
                )
            ).to.be.revertedWith("VCRegistry: not an authorized issuer");
        });
    });

    // -------------------------------------------------------------------
    // AuditTrail
    // -------------------------------------------------------------------
    describe("AuditTrail", () => {
        it("records and retrieves events", async () => {
            const hash    = hre.ethers.keccak256(hre.ethers.toUtf8Bytes('{"event":"test"}'));
            const did     = "did:ethr:0xUSER";
            const EventType = { DocumentVerified: 1 };

            await auditTrail.connect(bank).recordEvent(hash, did, EventType.DocumentVerified);

            expect(await auditTrail.totalEvents()).to.equal(1);
            const indices = await auditTrail.getEventsByDID(did);
            expect(indices.length).to.equal(1);

            const ev = await auditTrail.getEvent(0);
            expect(ev.dataHash).to.equal(hash);
            expect(ev.subjectDID).to.equal(did);
        });
    });
});
