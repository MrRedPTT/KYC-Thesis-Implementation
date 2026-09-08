require("@nomicfoundation/hardhat-ethers");
require("@nomicfoundation/hardhat-chai-matchers");
require("dotenv").config();

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
    solidity: {
        version: "0.8.20",
        settings: {
            optimizer: { enabled: true, runs: 200 },
        },
    },
    networks: {
        // Local development node — run: npx hardhat node
        hardhat: {
            chainId: 1337,
        },
        // Ethereum Sepolia public testnet (no real money required)
        sepolia: {
            url:      process.env.SEPOLIA_RPC_URL  || "",
            accounts: process.env.PRIVATE_KEY ? [process.env.PRIVATE_KEY] : [],
            chainId:  11155111,
        },
    },
    paths: {
        sources:   "./contracts",
        tests:     "./test",
        cache:     "./cache",
        artifacts: "./artifacts",
    },
};
