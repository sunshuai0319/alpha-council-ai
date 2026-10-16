# Alpha Council 合约

这是 Alpha Council 的 Hardhat + OpenZeppelin 合约开发目录，用于开发和测试链上存证合约。

## 环境变量

复制 `.env.example` 为 `.env`，填写只用于测试网的部署账户：

```bash
cp .env.example .env
```

变量名与 `treasury-sentinel/contracts` 的配置保持一致：
`BASE_SEPOLIA_RPC_URL`、`DEPLOYER_PRIVATE_KEY` 和可选的 `ETHERSCAN_API_KEY`。

不要提交 `.env`，也不要使用持有主网资产或生产资金的钱包。

## 常用命令

```bash
npm install
npm run compile
npm test
npm run gas
npm run gas:benchmark
npm run gas:simulate:base-sepolia
npm run deploy:base-sepolia
```

`npm run gas` 会运行 1、10、100、250、500、1000 个用户子树的本地测试和 Gas 报告。
`npm run gas:benchmark` 会针对每个用户数量输出一个具体的 `gasUsed` 值。
`npm run gas:simulate:base-sepolia` 会连接已部署的 Base Sepolia 合约，先执行多种规模的
`eth_estimateGas`，再提交一笔 10 个用户子树的测试批次。可以通过
`SIMULATION_USER_SUBTREES=1|10|100|250|500|1000` 修改实际提交的规模。

## 部署记录

公开的部署元数据保存在 [`deployments/<network>.json`](./deployments/) 中，包含网络、
chain ID、合约地址、部署者地址、部署交易哈希、区块号和时间戳，不包含私钥。

Base Sepolia 的部署记录见
[`deployments/baseSepolia.json`](./deployments/baseSepolia.json)。

Base Sepolia 的实际 Gas 模拟报告见
[`reports/baseSepolia-gas-simulation.json`](./reports/baseSepolia-gas-simulation.json)。

## Gas 基准

当前本地 Hardhat benchmark（不含 Base L1 data fee）：

| 用户子树数 | `commitBatch` Gas |
|---:|---:|
| 1 | 55,262 |
| 10 | 81,830 |
| 100 | 347,510 |
| 250 | 790,310 |
| 500 | 1,534,190 |
| 1,000 | 3,022,142 |

合约部署约 `398,549` Gas。上线前仍需在 Base Sepolia 使用 `eth_estimateGas` 和实发交易复测，
因为最终费用还会受到 Base L1 data fee、实时 gas price 和 calldata 大小影响。

## Base Sepolia 实测

2026-10-16 使用测试网合约提交了一笔虚拟批次：10 个用户子树、30 条叶子，实际消耗
`85,946 gas`，当时 gas price 为 `0.006 gwei`，交易费用为 `0.000000515676 ETH`。

同一份样例数据的 RPC 估算结果如下：

| 用户子树数 | 估算 Gas | 按当时 gas price 的估算费用 |
|---:|---:|---:|
| 1 | 56,847 | 0.000000341082 ETH |
| 10 | 86,870 | 0.000000521220 ETH |
| 100 | 389,185 | 0.000002335110 ETH |
| 250 | 891,415 | 0.000005348490 ETH |
| 500 | 1,734,502 | 0.000010407012 ETH |
| 1,000 | 3,420,916 | 0.000020525496 ETH |

上述价格是 Base Sepolia 测试网的实时值，只用于验证数量级，不能直接当作主网报价。
主网预算应按 `gasUsed × 实时 L2 gas price + Base L1 data fee` 计算。

## 合约设计

[`src/AttestationRegistry.sol`](./src/AttestationRegistry.sol) 只保存全局 Merkle root 和紧凑的
用户子树 root，具体 payload 保存在 IPFS。

用户补签使用离线 EIP-712 签名，不为每个用户创建链上交易。

Base Sepolia 使用 chain ID `84532`，默认 RPC 为 `https://sepolia.base.org`。完成 Sepolia
端到端验证和 Gas 检查后，再迁移到 Base Mainnet。
