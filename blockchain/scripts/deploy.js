const hre = require("hardhat");

async function main() {
    const [deployer] = await hre.ethers.getSigners();
    console.log("Deploying contracts with account:", deployer.address);

    // 1. DID Registry
    const DIDRegistry = await hre.ethers.deployContract("DIDRegistry");
    await DIDRegistry.waitForDeployment();
    const didRegistryAddress = await DIDRegistry.getAddress();
    console.log("DIDRegistry deployed to:", didRegistryAddress);

    // 2. VC Registry
    const VCRegistry = await hre.ethers.deployContract("VCRegistry");
    await VCRegistry.waitForDeployment();
    const vcRegistryAddress = await VCRegistry.getAddress();
    console.log("VCRegistry deployed to:", vcRegistryAddress);

    // 3. Audit Trail
    const AuditTrail = await hre.ethers.deployContract("AuditTrail");
    await AuditTrail.waitForDeployment();
    const auditTrailAddress = await AuditTrail.getAddress();
    console.log("AuditTrail deployed to:", auditTrailAddress);

    // 4. Register the deployer as an authorized issuer for local testing
    const authTx = await VCRegistry.authorizeIssuer(deployer.address);
    await authTx.wait();
    console.log("Deployer registered as authorized issuer in VCRegistry");

    // Print .env values for the backend
    console.log("\n--- Copy these into implementation/backend/.env ---");
    console.log(`DID_REGISTRY_ADDRESS=${didRegistryAddress}`);
    console.log(`VC_REGISTRY_ADDRESS=${vcRegistryAddress}`);
    console.log(`AUDIT_TRAIL_ADDRESS=${auditTrailAddress}`);
    console.log(`DEPLOYER_PRIVATE_KEY=<your Hardhat account private key>`);
    console.log(`BLOCKCHAIN_RPC_URL=http://127.0.0.1:8545`);
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
