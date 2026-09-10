# Web3 可验证决策路线

Web3 在 Alpha Council AI 中首先承担“证明与归属”角色，不承担交易执行、资产托管或收益承诺。

## 当前版本

保留链下 `DecisionAttestation` 结构：

```text
decision_id
report_hash
created_at
system_version
report_uri
```

目前只记录和展示普通审计字段，不部署合约、不连接用户钱包资金、不把原文、用户数据、行情明细或 WEEX 凭据写入 calldata。

## 后续可迭代方向

1. 在 EVM L2 部署极简 `DecisionAttestation` 合约，只登记报告 hash、版本和时间。
2. 用户用钱包签名自己的策略报告或复盘报告，形成可验证的作者归属。
3. 将策略版本、回测区间、风险规则和执行记录做成可公开验证的 report URI。
4. 在完全非托管前提下，探索策略订阅、研究报告和声誉积分；不发行没有实际用途的 token。

钱包地址只用于签名和身份关联，不获得 WEEX API secret，也不能绕过 Clerk 用户隔离和后端风险闸门。任何未来的链上组件都必须是交易路径之外的旁路证明服务。
