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
def _reason_code(reason: str) -> str:
    """指纹只用码本身，丢掉 `:` 后面的参数。

    `cycle.py:505` 写的是 f"account_unavailable:{exc}" —— 异常原文进了理由，
    "connection refused" 与 "timed out" 是两条不同字符串，不归一化的话指纹每轮
    都变、每轮都推。
    """
    return reason.split(":", 1)[0].strip()


def signal_signature(state: TradingCycleState, risk: RiskDecision) -> str:
    action = state.trade_proposal.action.value if state.trade_proposal else "NONE"
    codes = "|".join(sorted({_reason_code(reason) for reason in risk.reasons}))
    return f"{action}:{risk.status.value}:{codes}"
```

指纹只含决策语义，不含价格与信号分数。因此「SHORT 的 score 从 -0.37 漂到 -0.36」不重复推，
「SHORT 翻成 LONG」或「通过 翻成 拒绝」立刻推。`sorted(set(...))` 防御理由顺序抖动，
`_reason_code` 防御参数抖动。

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
- ~~暂停态走 HOLD 路径，已被 hold 过滤拦下。~~ **这条是错的，已修**：暂停态确实走
  HOLD 路径，但真正的问题在更前面 —— 暂停轮**根本不调用 `notify()`**（通知块嵌在
  非暂停分支里），过滤条件再宽松也拦不到没进来的调用。修法见下面「第三轮」：
  通知块提到 `if/else` 之外，同时让 HOLD 过滤放行 `risk.status is PAUSED`
  （暂停分支造的正是 `action=HOLD` + `status=PAUSED`，不放行就会被吞掉）。
  放行不刷屏：指纹 `HOLD:PAUSED:paused` 逐轮相同，每个品种只推第一条。
- 新增配置 `lark_notify_rejected_signals: bool = False`（`app/config.py`）。

**已知副作用（用户已确认接受）**：账户侧持续持有某品种时（如手动开的 ETH-USDT 仓），
该品种每轮都是 `position_already_open`，会被本规则全部拦掉，**该品种完全静默** ——
既无开仓建议也无「被持仓挡住」的提示。这是刻意的：notify 模式下系统本就无法动作，
推了也没有可执行的事，用户会在网页端看到完整记录。改变主意时把开关打开即可恢复推送。

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
   - `veto_fail_closed:` → 否决链异常，已安全观望：{detail}。来自 `agents/graph.py` 的
     `_hold_proposal(current, f"veto_fail_closed:{reasons}")`，语义是「否决链本身坏了」，
     与 `vetoed:`（「被否决」）不是一回事，所以单列。
3. **正则**（照抄 `labels.ts` 的 `RULE_SIGNAL`）
   - `rule signal SHORT score=-0.35` → **规则信号 做空，分数 -0.35**
   - ~~与前端唯一的措辞差异：前端保留英文 `SHORT`~~ **已统一**：前端也改成「做多 / 做空」
     （`reason.ruleSignalEntryLong` / `…Short`）。中文界面留一个英文 `SHORT` 属于漏译，
     没理由保留。

**认不出的码原样显示**，照搬前端那条原则：显示陌生码远好过显示空白，而且它本身就是
排查线索。

但「认不出」不能靠**手工挑码**判断 —— 上表第一版就是这么漏掉 9 个码的，`veto_fail_closed:`
更是因为只枚举了 `reasons=[...]` 形式、没覆盖 f-string 形式而漏掉。所以改成机器对齐：
`backend/tests/unit/test_reasons.py` 的 `test_card_table_never_exceeds_the_web_table`
从 `frontend/lib/labels.ts` 正则抽出 `REASON_KEYS` / `PREFIX_KEYS`，断言卡片译表不超出它。

接入点：`风控理由` 用 `reason_parts("、".join(risk.reasons))`，`策略说明` 用
`reason_parts(proposal.reasoning_summary)` —— **也必须是 `reason_parts` 而不是
`reason_text`**：`safe_hold` 写进摘要的永远是 `";".join(errors[-3:])`，整串丢给
`reason_text` 一个码都匹配不上，原样冒英文。LLM 生成的文本已由 `_language_instruction`
要求写简体中文，不需要处理。

`reason_parts` / `reasonParts` 都先 `trim()` 再过滤空段：后端两处拼接写法不同
（`cycle.py` 的 `_persist` 用 `";"`，`graph.py` 用 `"; "`），后者拆出来是 `" "`，
它是 truthy，`filter(Boolean)` 拦不住，既会多渲染一个分隔符，`" hold_no_order"` 也
匹配不上精确表。

## 测试计划

先写测试（失败），再实现。

`backend/tests/unit/test_reasons.py`（新增，实际 8 条）：精确表命中、前缀表带参数、
`signal_hold_score` 四舍五入、规则信号正则、未知码原样返回、`;` 拆分、中文散文穿透、
空输入。

`backend/tests/unit/test_lark.py`（补充，实际名称以文件为准）：

| 计划中的名字 | 实际实现 |
|---|---|
| `test_signal_signature_stable_across_price_changes` | `test_signal_signature_ignores_price_and_level_changes` |
| `test_signal_signature_ignores_reason_parameters` | 同名 |
| `test_duplicate_signal_is_not_sent_again` | `test_duplicate_signal_is_sent_once` |
| `test_changed_risk_status_is_sent_again` | 拆成 `test_halting_rejection_after_allowed_signal_is_sent`（通过 → 熔断）与 `test_allowed_signal_then_single_trade_rejection_stays_silent`（通过 → 单笔，即截图场景） |
| `test_failed_send_does_not_update_signature` | 同名 |
| `test_rejected_single_trade_reason_is_suppressed` | `test_single_trade_rejection_is_suppressed` |
| `test_rejected_halting_reason_is_sent` | `test_halting_rejection_is_sent` |
| `test_rejected_signal_sent_when_switch_enabled` | `test_single_trade_rejection_is_sent_when_switch_enabled` |
| `test_format_time_includes_beijing_time` | `test_subtitle_shows_utc_and_beijing_time` + `test_subtitle_beijing_time_carries_the_next_day` |
| `test_position_size_pct_is_rounded` / `test_prices_are_rounded_to_two_places` | 合并为 `test_position_size_and_prices_are_rounded` |
| —（计划外） | `test_integer_prices_keep_no_trailing_zeros`、`test_non_finite_numbers_do_not_break_rendering`、`test_card_translates_machine_codes` |

现有 `test_notifier_gets_one_token_and_sends_to_each_configured_recipient`（LONG 后接
SHORT）指纹不同，不受影响，保持通过。

## 译表缺口（已补齐）

第一版译表**只覆盖前端 `labels.ts` 已有的码**，事后审计发现后端有 9 个码两边都没译，
会原样显示成英文：

| 码 | 产生点 | 默认配置下会显示吗 |
|---|---|---|
| `exchange_position_missing` | `services/cycle.py:536` | 卡片不会（`REJECTED` 且 `halt=False`，被单笔拒绝抑制拦下），网页会 |
| `reentry_cooldown` | `services/cycle.py:546` | 同上 |
| `manual_reduce_only` | `services/cycle.py:1466` | 不走 `notify()`，只出现在网页 |
| `entry_signal_evidence_missing` | `agents/graph.py:620` | 卡片：`LARK_NOTIFY_HOLD=true` 时会；网页：会 |
| `entry_stop_loss_missing` | `agents/graph.py:616` | 同上 |
| `crossed_book` / `inverted_24h_range` / `last_price_outside_24h_range` / `spread_bps=N>M` | `agents/graph.py:485-493` | 卡片不会（进 `veto_verdicts`），**智囊团页会照原样渲染** |

`crossed_book` 等三条是**事后才发现**的：第一版只比对了「风控理由」用的码，漏了
`data_integrity_node` 往 evidence / `reasoning_summary` 里写的这组。教训是别手工挑码。

**收口办法**：`backend/tests/unit/test_reasons.py::test_card_table_never_exceeds_the_web_table`
解析 `frontend/lib/labels.ts`，断言卡片译表（`_REASON_TEXTS` + `_REASON_TEMPLATES`）是
网页译表的子集，前端源码不在时 skip（后端镜像里没有 `frontend/`）。
反向（后端发了码但没人译）无法用测试覆盖 —— 那要枚举所有产生点，太脆 —— 靠 code review。

另外，`labels.ts` 里的 `entry_evidence_missing` 是**后端改名前的旧键**（后端现在发
`entry_signal_evidence_missing`），前端必须留着：历史行还在库里。后端的译表只用于
渲染当下这张卡片，从不见历史，所以那一侧已删掉这个死键。

### 第二轮：不是「缺码」，而是「匹配方式不对」

第一轮补的是**枚举码**，补完仍然漏英文，因为漏的不是码，是匹配方式：

| 现象 | 根因 | 修法 |
|---|---|---|
| `策略说明` 整段冒英文 | `safe_hold` 写的是 `";".join(errors[-3:])`，整串丢给精确表一条都命中不了 | `build_trade_signal_card` / `ReasonText` / `reasonLabelText` 全部改用 `reason_parts` / `reasonParts` 拆开逐条翻译 |
| `veto_fail_closed:news_macro` 原样显示 | 前缀表没登记；第一轮的穷举只匹配 `reasons=[...]`，漏了 `f"veto_fail_closed:{reasons}"` 这种 f-string 构造 | 前缀表加 `veto_fail_closed:`，两边各一条文案 |
| 网页上 `规则信号 SHORT` | 前端 `RULE_SIGNAL` 把方向当参数原样插值，没收进译表 | 拆成 `ruleSignalEntryLong` / `…Short` 两条，与卡片统一 |
| `hold_no_order; ` 多一个分隔符 | `"; "` 拆出来是 `" "`，truthy，`filter(Boolean)` 拦不住 | 拆完先 `trim()` 再过滤 |

教训与前一轮同源：**漏译这件事不能靠人眼穷举匹配点**。第一轮漏了 3 条，第二轮又漏了
`veto_fail_closed:` —— 每次都是「我以为枚举全了」。所以对齐测试是唯一的止血办法，
新增译表必须同步进 `labels.ts`，否则 `test_card_table_never_exceeds_the_web_table` 红。

### 第三轮：熔断静默（不是翻译问题）

审计时顺带发现的独立缺陷：**账户进入 PAUSED 之后，通知彻底停止**。它不属于译表缺口 ——
卡片文案是对的，是**根本没推出去**。

第一版修在了错误的地方，值得记下来。当时的推理是：「暂停轮造 HOLD 提案，被 HOLD 过滤
吞掉」，于是给 HOLD 过滤加了 `risk.status is PAUSED` 放行。代码评审发现这是 **no-op**：

`LarkNotifier.notify` 在整条生产链路上**只有一个调用点**（`cycle.py` 的
`_best_effort("lark_notification", …)`），而它嵌在 `if halt_reason is not None: … else:`
的 **else 分支内部**。暂停轮走的是 `if` 分支，造完 `RiskDecision(status=PAUSED)` 就把
`execution` 置 `None` 收工，**从不调用 `notify()`**。过滤条件再宽松也拦不到没进来的调用。

于是真正的修法是两处：

1. **调用方**（根因）：把通知块移出 `else`，提到 `if/else` 之外 —— 与紧邻其后的对账
   同理，「暂停」不该成为不通知的理由。`trading_enabled=false` 走同一分支，此前
   更是一条都发不出来。
2. **过滤条件**：暂停分支造的正是 `action=HOLD` + `status=PAUSED`，不提这个条件就会被
   HOLD 过滤吞掉，所以两处都要改。

`risk.halt` 那一支保留为防御（HOLD 提案在 `_evaluate_proposal` 里提前返回
`hold_no_order`，`halt=True` + HOLD 当前不可达）：HOLD 过滤不该有权决定熔断要不要播报。

**测试教训**：第一版的两个测试直接调 `notify()`，于是修复前后都是绿的 —— 缺口在调用方，
绕过调用方的测试天然测不到。能捕获回归的是穿 `TradingCycleService.run()` 的集成测试
（`tests/api/test_cycle_service.py::test_paused_account_still_notifies_that_trading_is_stopped`，
已确认在只回退 `cycle.py` 时 `assert 0 == 1` 失败）。

## 不做的事

- 不改后端落库的机器码（审计标识）
- 不做「原地更新同一张卡片」（需存 message_id，且丢失历史变化）
- 不加静默期定时重推（去重后已足够安静，YAGNI）
- `_reason_code` 不做冒号截断（当前不可达）
- `close()` 不重置 `_client`（当前无调用点）
- 不给「每个决策都记指纹」—— 那会正好在信号抖动于阈值附近时破坏去重，
  而抖动正是当初要去重的原因
