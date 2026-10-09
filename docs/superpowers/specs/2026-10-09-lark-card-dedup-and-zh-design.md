# Lark 卡片去重与中文化设计

日期：2026-10-09
状态：已获用户确认

## 背景与目标

`notify` 模式下 worker 每个决策周期（实测 ~9 分钟，见 `docs/runbook.md` 的调度漂移记录）
对每个 `(用户, 品种)` 无条件推送一张 Lark 卡片（`app/services/cycle.py:360-388`）。
ETH-USDT 连续三轮都是 SHORT 时，卡片内容几乎一样 —— 只有价格微动、风控状态从
「通过」翻成「拒绝 position_already_open」—— 群里被同一信号刷屏。

截图同时暴露了三个渲染问题：

1. 副标题只有 UTC 时间（`10-09 01:37 UTC`），国内读者要心算 +8。
2. `仓位比例 27.073999999999998%` —— 浮点误差。
3. `参考止损价 2565.108051` —— 小数位过长。
4. `风控理由` 与 `策略说明` 直接显示后端机器码（`position_already_open`、
   `rule signal SHORT score=-0.35`）。

目标：同一决策只打扰一次；卡片全中文可读；时间同时给出 UTC 与北京时间。

## 现状事实（实现前已核对）

- **后端刻意存英文机器码。** `frontend/lib/labels.ts` 顶部注释写明「后端存的是稳定的
  英文机器码……那是审计和查询要用的标识，不该为了显示而本地化」。网页端早就有完整
  翻译层（`reasonLabel` / `reasonParts`），**只有 Lark 卡片漏了这层翻译**。所以第 4 项
  在卡片层补译表，**不改后端落库字符串**（改了会污染 `trading_decisions` 审计记录，
  也破坏前后端契约）。
- **风控状态的语义**（`app/risk/engine.py`、`app/domain/schemas.py:185`）：
  - `ALLOWED`（通过）→ `allowed == True`，`execute` 模式下 `ExecutionService` 只接受
    这种提案，**真会下单**。
  - `REJECTED`（拒绝）→ 硬拦，不下单。
  - `PAUSED`（暂停）→ 账户熔断暂停。
  - **「通过」≠「开仓」**：HOLD 提案也返回 `ALLOWED`（`cycle.py:499`，理由
    `hold_no_order`）。判断是否为开仓建议要看**动作**，不能只看风控状态。
  - `RiskDecision.halt` 已由风控引擎算好，恰为账户级熔断（日亏损 / 连亏 / 权益非正），
    无需新造分类。
- **所有 `risk.reasons` 都是静态枚举串**，不夹带数值，因此可作指纹。
- `app/risk/engine.py` 里 `paused=True` 分支实际是死代码（唯一调用点
  `cycle.py:562` 写死 `paused=False`）；暂停走 `cycle.py:282` 把提案替换成 HOLD 的路径。

## 决策

| # | 决策 | 选定方案 |
|---|---|---|
| 1 | 去重策略 | **决策指纹去重**：指纹 = 动作 + 风控状态 + 风控理由，与上次**成功推送**的指纹相同则静默跳过 |
| 2 | 指纹存哪 | **进程内存**（`LarkNotifier` 实例字典），零迁移；worker 重启最多多发一条 |
| 3 | 拒绝卡片 | **熔断类必推，单笔类默认不推**，留配置开关可打开 |
| 4 | 时间格式 | `10-09 01:37 UTC（北京 10-09 09:37）`，**北京时间带日期**避免跨日歧义 |
| 5 | 渲染瑕疵 | 仓位比例量化 2 位小数；价格类字段保留 2 位小数 |
| 6 | 中文化 | 卡片层新增译表，措辞与前端 `i18n.tsx` zh-CN 同源 |

## 设计

### 改动范围

只动 `backend/app/notifications/`：

- 新增 `backend/app/notifications/reasons.py` —— 机器码 → 中文译表（纯数据 + 纯函数）
- 修改 `backend/app/notifications/lark.py` —— 去重、时间、数字格式、接入译表
- `cycle.py`、数据库、前端**均不改**

`cycle.py` 无需改动的理由：`notify()` 返回 `False` 时现有代码已记 `result=skipped`，
与 HOLD 跳过走同一条路。

### 1. 决策指纹

模块级纯函数，可独立单测：

```python
def signal_signature(state: TradingCycleState, risk: RiskDecision) -> str:
    action = state.trade_proposal.action.value if state.trade_proposal else "NONE"
    reasons = "|".join(sorted(set(risk.reasons)))
    return f"{action}:{risk.status.value}:{reasons}"
```

指纹只含决策语义，不含价格与信号分数。因此「SHORT 的 score 从 -0.37 漂到 -0.36」不重复推，
「SHORT 翻成 LONG」或「通过 翻成 拒绝」立刻推。`sorted(set(...))` 防御理由顺序抖动。

`LarkNotifier` 新增 `self._last_signatures: dict[tuple[str, str], str]`，
key 为 `(user_id, symbol)`。

`notify()` 流程：

```
动作是 HOLD 且未开启 hold 通知        → 跳过（不变）
未配置                                → 跳过（不变）
是单笔类拒绝且未开启开关               → 跳过，日志 reason=rejected_signal_suppressed   ← 新增
指纹 == 上次已推送的指纹               → 跳过，日志 reason=duplicate_signal             ← 新增
构建卡片并发送给全部收件人
全部成功后才写入 _last_signatures[key]                                                 ← 新增
```

两个刻意的选择：

- **只在真正推送成功时更新指纹。** 所以「SHORT 通过 → HOLD(跳过) → SHORT 通过」只推一条：
  HOLD 本来就不推，用户视角信号一直是 SHORT。反过来若每轮无条件更新指纹，规则信号在
  阈值附近抖动时照样刷屏，等于没修。
- **发送抛异常时不更新指纹。** 宁可下轮重发一条，也不要因一次网络抖动把新信号永久吞掉。

### 2. 拒绝卡片的抑制规则

```
跳过条件：risk.status is REJECTED and not risk.halt and not settings.lark_notify_rejected_signals
```

- 熔断类（`risk.halt == True`）**永远推** —— 它代表「系统停手了」，漏掉最危险。
- 单笔类（`position_already_open`、`max_notional`、`reward_risk_too_low`…）默认不推。
- 暂停态走 HOLD 路径，已被 hold 过滤拦下。
- 新增配置 `lark_notify_rejected_signals: bool = False`（`app/config.py`）。

### 3. 时间

```python
_BEIJING = timezone(timedelta(hours=8))

def _format_time(milliseconds: int) -> str:
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    beijing = moment.astimezone(_BEIJING)
    return f"{moment.strftime('%m-%d %H:%M')} UTC（北京 {beijing.strftime('%m-%d %H:%M')}）"
```

北京时间**带日期**：UTC 16:00 之后北京已跨日，`10-09 20:00 UTC` 只写「北京 04:00」
会被误读成当天已过去的凌晨。格式不随时刻变化，保持一致。

### 4. 数字格式

- 仓位比例：`Decimal(str(size_pct)).quantize(Decimal("0.01"))` → `27.07%`
- 价格类字段（参考下单价格 / 参考止损价 / 参考止盈价）：保留 2 位小数 → `2565.11`

实现为 `_format_number(value, places=None)`：先 `quantize` 到 `places` 位小数，**再走
现有的去尾零逻辑**。所以 `27.07%`、`27%`、`2565.11`、`97` —— 整数价格仍渲染成 `97`
而不是 `97.00`。`places=None`（杠杆等字段）完全保持现有行为。

### 5. 中文化（`reasons.py`）

```python
def reason_text(code: str) -> str:   # 单条码 → 中文
def reason_parts(text: str) -> str   # 按 ";" 拆开逐条翻译再拼回
```

三级匹配，与 `frontend/lib/labels.ts` 一一对应：

1. **精确表**（约 25 条，措辞照抄 `frontend/lib/i18n.tsx` zh-CN，保证卡片与网页同一句话）
   - `position_already_open` → 该品种已有持仓，不再加仓
   - `market_data_stale` → 行情数据过期
   - `daily_loss_limit` → 当日亏损触发熔断
   - `stop_loss_required` → 缺少止损
   - …（含 `REASON_KEYS` 全表）
2. **前缀表**（带参数，照抄 `labels.ts` 的 `PREFIX_KEYS`）
   - `signal_hold_score_0.26` → 信号分 0.26，未达开仓阈值
   - `market_data_unavailable:ETH-USDT/12h` → 行情获取失败：ETH-USDT/12h
   - `account_unavailable:` / `retrieval_failed:` / `vetoed:` 同理
3. **正则**（照抄 `labels.ts` 的 `RULE_SIGNAL`）
   - `rule signal SHORT score=-0.35` → **规则信号 做空，分数 -0.35**
   - 与前端唯一的措辞差异：前端保留英文 `SHORT`，卡片用「做空」—— 卡片通篇无英文，
     且表头标签已经是「做空」，统一更顺。这是有意为之。

**认不出的码原样显示**，照搬前端那条原则：显示陌生码远好过显示空白，而且它本身就是
排查线索。因此 `inverted_24h_range` 这类未收录码保持原样，不臆造译文。

接入点：`风控理由` 用 `reason_parts("、".join(risk.reasons))`，`策略说明` 用
`reason_text(proposal.reasoning_summary)`。LLM 生成的文本已由 `_language_instruction`
要求写简体中文，不需要处理。

## 测试计划

先写测试（失败），再实现。

`backend/tests/unit/test_reasons.py`（新增）：

- 精确表命中：`position_already_open` → 中文
- 前缀表命中且参数正确：`signal_hold_score_0.26000000000001` → 「信号分 0.26，…」
- 规则信号正则：`rule signal SHORT score=-0.5477` → 「规则信号 做空，分数 -0.55」
- 未知码原样返回
- `reason_parts` 按 `;` 拆分并逐条翻译

`backend/tests/unit/test_lark.py`（补充）：

- `test_signal_signature_stable_across_price_changes` —— 价格/分数变了、动作没变 → 指纹相同
- `test_duplicate_signal_is_not_sent_again` —— 连发两次相同决策，只发一条消息
- `test_changed_risk_status_is_sent_again` —— 通过 → 拒绝 要再推
- `test_failed_send_does_not_update_signature` —— 首次发送失败后，相同决策下次仍会尝试
- `test_rejected_single_trade_reason_is_suppressed` —— `position_already_open` 默认不推
- `test_rejected_halting_reason_is_sent` —— `halt=True` 的拒绝仍然推
- `test_rejected_signal_sent_when_switch_enabled` —— 开关打开后单笔拒绝照推
- `test_format_time_includes_beijing_time` —— 含 UTC 与北京时间，覆盖跨日用例
- `test_position_size_pct_is_rounded` / `test_prices_are_rounded_to_two_places`
- 现有 `test_notifier_gets_one_token_and_sends_to_each_configured_recipient`（LONG 后接
  SHORT）指纹不同，不受影响，应保持通过

## 不做的事

- 不改后端落库的机器码（审计标识）
- 不改前端（前端翻译层已完备）
- 不做「原地更新同一张卡片」（需存 message_id，且丢失历史变化）
- 不加静默期定时重推（去重后已足够安静，YAGNI）
