const { ethers } = require("hardhat");

function subtrees(count) {
  return Array.from({ length: count }, (_, index) => ({
    user: ethers.getAddress(`0x${(index + 1).toString(16).padStart(40, "0")}`),
    userRoot: ethers.zeroPadValue(ethers.toBeHex(index + 1), 32),
    leafCount: 1
  }));
}

async function main() {
  const [owner] = await ethers.getSigners();
  const Registry = await ethers.getContractFactory("AttestationRegistry");
  const deployment = await Registry.deploy(owner.address);
  const deployReceipt = await deployment.deploymentTransaction().wait();
  console.log(`deployment gas=${deployReceipt.gasUsed}`);

  const root = ethers.keccak256(ethers.toUtf8Bytes("global-root"));
  for (const userCount of [1, 10, 100, 250, 500, 1000]) {
    const registry = await Registry.deploy(owner.address);
    await registry.waitForDeployment();
    const tx = await registry.commitBatch(
      1,
      root,
      "bafybeigdyrzt5example",
      userCount,
      1,
      2,
      1,
      subtrees(userCount)
    );
    const receipt = await tx.wait();
    console.log(`users=${userCount} gas=${receipt.gasUsed}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
