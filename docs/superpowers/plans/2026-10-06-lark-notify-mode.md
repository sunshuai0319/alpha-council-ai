# Lark 通知模式与自动交易开关实现计划

## 目标

默认将 worker 从自动开仓、自动下单止盈止损改为只生成决策并推送 Lark 国际版 interactive 卡片；保留原自动交易链路，可通过配置显式切回。当前本地开发无公网 HTTPS/域名，因此不依赖回调服务器；如未来需要事件订阅，采用 Lark 长连接。

## 任务

1. **配置与安全边界**
   - 新增 `TRADING_EXECUTION_MODE=notify|execute`，默认 `notify`。
   - 新增 Lark 国际版 API 地址、`app_id`、`app_secret`、收件人 ID 与类型、超时配置。
   - `.env.example` 与运行文档说明：密钥只放本地 `.env`；原自动交易通过 `execute` 恢复。

2. **Lark 卡片通知客户端**
   - 使用 `https://open.larksuite.com` 的租户 token 接口与消息发送接口。
   - 发送 interactive 卡片，展示状态、品种、方向、参考下单价格、止损/止盈参考价、仓位比例、杠杆、风险结论和理由。
   - 缺少收件人或 Lark 配置时安全跳过并记录日志；接口失败不阻断决策落库。

3. **worker 执行模式接入**
   - `notify` 模式不调用开仓/平仓订单，也不运行 `PositionManager` 自动退出；保留行情采集、信号、风控、对账观测和决策落库。
   - `execute` 模式保持当前开仓、交易所止损止盈、软件持仓管理行为。
   - 通知发送发生在风险评估后，卡片包含当前参考价格和 proposal 参数。

4. **测试与验证**
   - 添加配置、卡片 payload、token 缓存/消息发送、通知模式不下单不管理持仓、execute 模式兼容性测试。
   - 运行受影响的 pytest、ruff，并检查 git diff 和 tag 指向。

## 验证条件

- `uv run pytest tests/unit/test_config.py tests/unit/test_lark.py tests/integration/test_execution.py tests/api/test_cycle_service.py -q` 全部通过。
- `uv run ruff check app tests workers` 通过。
- `git tag --points-at alpha-council-ai-pre-lark-notify-20261006` 指向修改前基线提交。
