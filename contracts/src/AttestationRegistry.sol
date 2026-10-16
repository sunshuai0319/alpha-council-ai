// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title Alpha Council attestation registry
/// @notice Stores only batch roots and compact user subtree roots. Payloads stay in IPFS.
/// @dev User endorsements are deliberately off-chain EIP-712 signatures; there is no
///      per-user endorsement transaction in this contract.
contract AttestationRegistry is Ownable {
    struct UserSubtreeInput {
        address user;
        bytes32 userRoot;
        uint32 leafCount;
    }

    event BatchCommitted(
        uint256 indexed batchId,
        bytes32 globalRoot,
        string manifestCid,
        uint32 leafCount,
        uint64 windowStart,
        uint64 windowEnd,
        uint16 schemaVersion
    );

    event UserSubtree(
        uint256 indexed batchId,
        address indexed user,
        bytes32 userRoot,
        uint32 leafCount
    );

    error InvalidBatchId(uint256 expected, uint256 received);
    error EmptyRoot();

    uint256 public lastBatchId;

    constructor(address initialOwner) Ownable(initialOwner) {}

    /// @notice Commits one global Merkle root and all user roots in one transaction.
    /// @param batchId Strictly increasing application batch identifier.
    /// @param globalRoot Root of the two-level Merkle tree.
    /// @param manifestCid IPFS CID for the public manifest and encrypted payload index.
    /// @param leafCount Number of leaves covered by the global root.
    /// @param subtrees Per-user roots used to generate user inclusion proofs.
    function commitBatch(
        uint256 batchId,
        bytes32 globalRoot,
        string calldata manifestCid,
        uint32 leafCount,
        uint64 windowStart,
        uint64 windowEnd,
        uint16 schemaVersion,
        UserSubtreeInput[] calldata subtrees
    ) external onlyOwner {
        if (globalRoot == bytes32(0)) revert EmptyRoot();
        uint256 expectedBatchId = lastBatchId + 1;
        if (batchId != expectedBatchId) {
            revert InvalidBatchId(expectedBatchId, batchId);
        }

        lastBatchId = batchId;
        emit BatchCommitted(
            batchId,
            globalRoot,
            manifestCid,
            leafCount,
            windowStart,
            windowEnd,
            schemaVersion
        );

        for (uint256 i = 0; i < subtrees.length; ++i) {
            UserSubtreeInput calldata subtree = subtrees[i];
            if (subtree.userRoot == bytes32(0)) revert EmptyRoot();
            emit UserSubtree(batchId, subtree.user, subtree.userRoot, subtree.leafCount);
        }
    }
}
