const fs = require("node:fs");
const path = require("node:path");
const hre = require("hardhat");

const deploymentPath = path.join(__dirname, "..", "deployments", "baseSepolia.json");
const reportPath = path.join(__dirname, "..", "reports", "baseSepolia-gas-simulation.json");

function makeUserAddress(index, deployer) {
  if (index === 0) return deployer;
  return hre.ethers.getAddress(`0x${index.toString(16).padStart(40, "0")}`);
}

function makeSubtrees(count, deployer) {
  return Array.from({ length: count }, (_, index) => ({
    user: makeUserAddress(index, deployer),
    userRoot: hre.ethers.keccak256(
      hre.ethers.toUtf8Bytes(`alpha-council-base-sepolia-gas-simulation-user-${index}`)
    ),
    leafCount: index + 1
  }));
}

function formatEth(value) {
  return hre.ethers.formatEther(value);
}

async function main() {
  if (!fs.existsSync(deploymentPath)) {
    throw new Error(`Deployment record not found: ${deploymentPath}`);
  }

  const deployment = JSON.parse(fs.readFileSync(deploymentPath, "utf8"));
  const [signer] = await hre.ethers.getSigners();
  const registry = await hre.ethers.getContractAt("AttestationRegistry", deployment.address, signer);
  const network = await hre.ethers.provider.getNetwork();
  const balance = await hre.ethers.provider.getBalance(signer.address);
  const feeData = await hre.ethers.provider.getFeeData();
  const gasPrice = feeData.gasPrice;
  if (!gasPrice) throw new Error("RPC did not return a gas price");

  const lastBatchId = await registry.lastBatchId();
  const batchId = lastBatchId + 1n;
  const windowEnd = Math.floor(Date.now() / 1000);
  const windowStart = windowEnd - 3600;
  const manifestCid = "bafybeigasimulationbase sepolia".replace(" ", "");
  const globalRoot = hre.ethers.keccak256(
    hre.ethers.toUtf8Bytes(`alpha-council-base-sepolia-gas-simulation-batch-${batchId}`)
  );
  const schemaVersion = 1;
  const leafCountPerUser = 3;
  const estimateCounts = [1, 10, 100, 250, 500, 1000];
  const estimates = [];

  for (const count of estimateCounts) {
    const subtrees = makeSubtrees(count, signer.address);
    const gas = await registry.commitBatch.estimateGas(
      batchId,
      globalRoot,
      manifestCid,
      count * leafCountPerUser,
      windowStart,
      windowEnd,
      schemaVersion,
      subtrees
    );
    const estimatedFee = gas * gasPrice;
    estimates.push({
      userSubtrees: count,
      gas: gas.toString(),
      gasPriceWei: gasPrice.toString(),
      estimatedFeeWei: estimatedFee.toString(),
      estimatedFeeEth: formatEth(estimatedFee)
    });
  }

  const sendCount = Number(process.env.SIMULATION_USER_SUBTREES || 10);
  if (!estimateCounts.includes(sendCount)) {
    throw new Error(`SIMULATION_USER_SUBTREES must be one of: ${estimateCounts.join(", ")}`);
  }
  const subtrees = makeSubtrees(sendCount, signer.address);
  const tx = await registry.commitBatch(
    batchId,
    globalRoot,
    manifestCid,
    sendCount * leafCountPerUser,
    windowStart,
    windowEnd,
    schemaVersion,
    subtrees
  );
  const receipt = await tx.wait();
  const actualGasPrice = receipt.gasPrice ?? gasPrice;
  const actualFee = receipt.gasUsed * actualGasPrice;
  const report = {
    contract: deployment.contract,
    network: hre.network.name,
    chainId: network.chainId.toString(),
    contractAddress: deployment.address,
    deployer: signer.address,
    batchId: batchId.toString(),
    sample: {
      manifestCid,
      globalRoot,
      leafCount: sendCount * leafCountPerUser,
      userSubtrees: sendCount,
      leafCountPerUser,
      windowStart,
      windowEnd,
      schemaVersion
    },
    deployerBalanceBeforeWei: balance.toString(),
    gasPriceWei: gasPrice.toString(),
    gasPriceGwei: hre.ethers.formatUnits(gasPrice, "gwei"),
    estimates,
    actualTransaction: {
      hash: tx.hash,
      blockNumber: receipt.blockNumber,
      status: receipt.status,
      gasUsed: receipt.gasUsed.toString(),
      effectiveGasPriceWei: actualGasPrice.toString(),
      effectiveGasPriceGwei: hre.ethers.formatUnits(actualGasPrice, "gwei"),
      feeWei: actualFee.toString(),
      feeEth: formatEth(actualFee),
      explorerUrl: `https://sepolia.basescan.org/tx/${tx.hash}`
    },
    recordedAt: new Date().toISOString(),
    notes: [
      "估算和实际交易均来自 Base Sepolia RPC；费用仅为测试网 ETH。",
      "Base L1 data fee 未单独拆分，最终主网费用需按实时 L1 data fee 复测。",
      "样例 payload 使用虚拟 root、CID 和测试地址，不代表真实业务数据。"
    ]
  };

  fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
