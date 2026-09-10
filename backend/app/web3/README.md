# Web3 边界

此目录只保留产品化方向的接口说明。当前版本不部署合约、不调用链上 RPC、不让钱包签署交易，也不把钱包连接到 WEEX 执行路径。

未来可在这里实现链下 `DecisionAttestation` DTO 和 EVM L2 hash 登记适配器。登记内容应只包含：

- `decision_id`
- `report_hash`
- `created_at`
- `system_version`
- `report_uri`

原始行情、用户业务数据、模型密钥、Clerk token 和 WEEX 凭据禁止进入 calldata。详见 [`docs/web3-roadmap.md`](../../../docs/web3-roadmap.md)。
