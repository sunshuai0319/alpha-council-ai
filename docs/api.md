# Alpha Council AI API

Base URL：`http://localhost:8000/api`

除 `/health` 外，业务接口都需要 Clerk Session Token：

```http
Authorization: Bearer <clerk-session-token>
```

API 从 token 的 Clerk `sub` 映射本地用户；客户端不能通过请求体指定其他 user id。

## 公开接口

### `GET /health`

```json
{"status":"ok","environment":"development","weex_mode":"virtual"}
```

## Dashboard 查询

以下接口均返回 `{"items": [...]}`，数据按当前用户隔离：

- `GET /market`：最近行情快照。行情事实是公共市场数据，但接口仍要求登录以保持控制台一致性。
- `GET /decisions`：当前用户的委员会决策、proposal、risk decision 和 execution result。
- `GET /portfolio`：当前用户的虚拟仓位。
- `GET /events`：当前用户的风控事件。
- `GET /auth/me`：当前 Clerk 用户及本地用户 id。

决策对象示例：

```json
{
  "id": "local-decision-id",
  "cycle_id": "cycle-id",
  "symbol": "BTC-USDT",
  "action": "HOLD",
  "status": "ALLOWED",
  "proposal": {
    "action": "HOLD",
    "position_size_pct": 0,
    "leverage": 1,
    "reasoning_summary": "committee_llm_not_configured"
  },
  "risk_decision": {"status": "ALLOWED", "reasons": ["hold_no_order"]},
  "execution_result": null,
  "created_at": "2026-09-10T00:00:00"
}
```

## 控制和人工操作

- `POST /control/pause` → `{"status":"PAUSED"}`
- `POST /control/resume` → `{"status":"RUNNING"}`
- `POST /positions/{symbol}/close`：请求当前用户对应 virtual 账户的 reduce-only 平仓。没有仓位时返回 `NO_POSITION`。

Dashboard 对 Pause、Resume 和 Close 都要求浏览器二次确认；这只是用户体验层保护，后端仍以风控和 virtual-only 配置为最终边界。

## 测试接口

`POST /test/run-cycle` 仅在 `APP_ENV=test` 下注册有效行为，并且必须带：

```http
X-Test-Exchange: fixture
```

fixture 周期默认生成 `HOLD`，使用固定 WEEX-like 数据，不触发网络请求和真实订单。缺少测试环境或请求头时分别返回 404/400。

## 错误约定

- `401`：缺少或无法验证 Clerk token。
- `400`：测试接口缺少 fixture header 等请求参数错误。
- `404`：禁用的测试支持或不存在的资源。
- `5xx`：仅表示 API 级别异常；交易周期内部依赖异常应优先落为 HOLD/拒绝并写入审计数据。
