const fs = require("node:fs");
const path = require("node:path");
const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  if (!deployer) {
    throw new Error("DEPLOYER_PRIVATE_KEY is missing or invalid in contracts/.env");
  }

  const registry = await hre.ethers.deployContract("AttestationRegistry", [deployer.address]);
  await registry.waitForDeployment();
  const address = await registry.getAddress();
  const deploymentTransaction = registry.deploymentTransaction();
  const receipt = deploymentTransaction ? await deploymentTransaction.wait() : null;
  const network = await hre.ethers.provider.getNetwork();
  const explorerBase = network.chainId === 84532n
    ? "https://sepolia.basescan.org"
    : network.chainId === 8453n
      ? "https://basescan.org"
      : null;
  const deployment = {
    contract: "AttestationRegistry",
    network: hre.network.name,
    address,
    deployer: deployer.address,
    chainId: network.chainId.toString(),
    deploymentTxHash: deploymentTransaction?.hash ?? null,
    deploymentBlock: receipt?.blockNumber ?? null,
    explorerAddressUrl: explorerBase ? `${explorerBase}/address/${address}` : null,
    explorerTxUrl: deploymentTransaction && explorerBase
      ? `${explorerBase}/tx/${deploymentTransaction.hash}`
      : null,
    deployedAt: new Date().toISOString()
  };

  const deploymentsDir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(deploymentsDir, { recursive: true });
  fs.writeFileSync(
    path.join(deploymentsDir, `${hre.network.name}.json`),
    `${JSON.stringify(deployment, null, 2)}\n`
  );
  console.log(JSON.stringify(deployment));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
