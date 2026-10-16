const { expect } = require("chai");
const { ethers } = require("hardhat");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

function subtrees(count) {
  return Array.from({ length: count }, (_, index) => ({
    user: ethers.getAddress(`0x${(index + 1).toString(16).padStart(40, "0")}`),
    userRoot: ethers.zeroPadValue(ethers.toBeHex(index + 1), 32),
    leafCount: 1
  }));
}

async function commit(registry, userCount) {
  return registry.commitBatch(
    1,
    ethers.keccak256(ethers.toUtf8Bytes("global-root")),
    "bafybeigdyrzt5example",
    userCount,
    1,
    2,
    1,
    subtrees(userCount)
  );
}

describe("AttestationRegistry", function () {
  it("commits a batch and emits one event per user subtree", async function () {
    const [owner] = await ethers.getSigners();
    const Registry = await ethers.getContractFactory("AttestationRegistry");
    const registry = await Registry.deploy(owner.address);
    await registry.waitForDeployment();

    await expect(commit(registry, 2))
      .to.emit(registry, "BatchCommitted")
      .withArgs(1, anyValue, "bafybeigdyrzt5example", 2, 1, 2, 1);
    expect(await registry.lastBatchId()).to.equal(1);
  });

  it("rejects non-owner commits", async function () {
    const [owner, other] = await ethers.getSigners();
    const Registry = await ethers.getContractFactory("AttestationRegistry");
    const registry = await Registry.deploy(owner.address);
    await registry.waitForDeployment();

    await expect(commit(registry.connect(other), 1))
      .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
      .withArgs(other.address);
  });

  it("rejects a replayed batch id", async function () {
    const [owner] = await ethers.getSigners();
    const Registry = await ethers.getContractFactory("AttestationRegistry");
    const registry = await Registry.deploy(owner.address);
    await registry.waitForDeployment();
    await commit(registry, 1);

    await expect(registry.commitBatch(
      1,
      ethers.keccak256(ethers.toUtf8Bytes("global-root-2")),
      "bafybeigdyrzt5example",
      1,
      1,
      2,
      1,
      subtrees(1)
    )).to.be.revertedWithCustomError(registry, "InvalidBatchId").withArgs(2, 1);
  });

  for (const userCount of [1, 10, 100, 250, 500, 1000]) {
    it(`gas benchmark: ${userCount} user subtrees`, async function () {
      const [owner] = await ethers.getSigners();
      const Registry = await ethers.getContractFactory("AttestationRegistry");
      const registry = await Registry.deploy(owner.address);
      await registry.waitForDeployment();
      const tx = await commit(registry, userCount);
      await tx.wait();
    });
  }
});
