# 中文 i18n 文案优化设计

日期：2026-09-11
状态：已获用户确认

## 背景与目标

前端（Next.js，`frontend/lib/i18n.tsx` 双语言字典）的中文文案存在两处问题：

1. 概念直译生硬，用户难懂。最典型的是把英文 *committee / council* 直译成「委员会」，带官僚味，普通用户看不出它和产品的关系。
2. 一批英文硬编码在组件里，中文模式下会直接冒出来（如 `trading account`、`market agent`）。

产品实际逻辑（后端 `agents/graph.py`）：三个 AI 分析师（行情/量化/宏观）各自输出判断 → 一个 AI 汇总成交易提案 → 风控闸门把关 → 仅在 WEEX 虚拟盘模拟执行。

## 决策

- **核心概念名**：委员会 → **AI 智囊团**（用户选定；贴合「交易实验室」气质，人人能懂）。
- **三个智能体** → **分析师**（行情分析师 / 量化分析师 / 宏观分析师）。
- **文案调性**：保留设计感，但用自然中文，不做逐字直译。
- **样式**：保持自定义 CSS，不迁移 Tailwind（规模小、收益低、有视觉漂移风险）。布局与视觉完全不变。
- en-US 文案一律不改；新增 key 的英文值等于当前硬编码英文原文。

## 改动范围

全部在 `frontend/lib/i18n.tsx`（改 key + 新增 key）与少量组件（用 `t()` 替换硬编码英文，日期本地化）。

### A. 委员会 → AI 智囊团

| key | 现在 | 改为 |
|---|---|---|
| nav.committee | AI 委员会 | AI 智囊团 |
| overview.title | 委员会正在观察。 | AI 智囊团值守中。 |
| overview.committee | 委员会 | 智囊团 |
| overview.lastAction | 委员会最近动作 | 智囊团最近动作 |
| overview.whatDecided | 委员会的决定 | 智囊团的决定 |
| overview.viewCommittee | 查看委员会 | 查看智囊团 |
| committee.title | AI 委员会 | AI 智囊团 |
| committee.description | 三个独立视角，一个结构化提案。模型可以解释交易，但不能绕过风险引擎。 | 三位分析师各持一种视角，最终合成一个交易提案。模型可以解释交易，但不能绕过风控引擎。 |
| committee.noMeeting | 委员会尚未召开 | 智囊团还未做出首次决策 |
| console.committeePaused | 委员会已暂停 | 智囊团已暂停 |
| console.committeeRunning | 委员会运行中 | 智囊团运行中 |
| trades.description | …委员会决策记录… | …智囊团决策记录… |
| trades.calls | 委员会调用 | 智囊团决策 |
| common.latestCall | 最近委员会调用 | 智囊团最近决策 |

### B. 智能体 → 分析师

| key | 现在 | 改为 |
|---|---|---|
| landing.debateTitle | 三智能体委员会 | 三位 AI 分析师共商 |
| landing.debateBody | 市场、量化和宏观视角会在生成提案前输出结构化推理。 | 行情、量化、宏观三位分析师各出判断，之后再汇总成交易提案。 |
| overview.heroBody | 市场、量化和宏观智能体并行讨论。风险决定讨论是否会变成订单。 | 行情、量化、宏观三位分析师并行研判，风控决定这场讨论能否变成订单。 |
| market.description | 交给智能体的精确市场状态… | 交给 AI 分析师的精确市场状态… |
| market.candles | 提供给委员会的 K 线 | 提供给智囊团的 K 线 |
| committee.noMeetingBody | 每个智能体的推理和置信度… | 每位分析师的推理和置信度… |

### C. 其它直译修正

| key | 现在 | 改为 |
|---|---|---|
| landing.title | 有边界的<br/>确定性。 | 有边界的<br/>笃定。 |
| landing.observeTitle | 新鲜的交易所上下文 | 来自交易所的实时行情 |
| landing.observeBody | …会被记录为带时间戳的事实。 | …会作为带时间戳的事实被记录下来。 |
| landing.constrainTitle | 风险拥有否决权 | 风控拥有一票否决权 |
| landing.constrainBody | …会在执行前安全失败。 | …会在下单前安全拦截。 |
| overview.liveSurface | 实时决策面 | 实时决策台 |
| overview.heroTitle | 信号只有在<br/><em>有边界时才有用。</em> | 信号有边界，<br/><em>才有意义。</em> |
| overview.telemetry | 当前遥测 | 实时行情 |
| overview.systemReadout | 系统读数 | 系统状态 |
| overview.riskGate | 风险闸门 | 风控闸门 |
| market.topOfBook | 盘口顶层 | 盘口最优档 |
| market.contract | 采集器约定 | 数据采集承诺 |
| market.freshness | 数据新鲜度规则 | 数据时效规则 |
| market.maxAge | 提案被拒绝前的最大市场数据年龄 | 超过该时效，提案将被拒绝 |
| market.endpoint | 仅虚拟端点；无生产订单路径 | 仅虚拟盘接入，无实盘下单通道 |
| common.noPositionsBody | 风险闸门允许非 HOLD 提案后… | 风控闸门放行非 HOLD 提案后… |
| events.failClosed | 安全失败 | 异常即拒绝 |
| console.riskLimits | 风控偏好 | 风控上限 |
| console.riskLimitsSaved | 风控偏好已保存。 | 风控上限已保存。 |
| console.riskLimitsFailed | 无法保存风控偏好。 | 无法保存风控上限。 |

### D. 硬编码英文 i18n 化（新增 key，中英各一份）

- 账户卡（console-page.tsx）：`trading account` → 交易账户；`WEEX virtual account` → WEEX 虚拟账户；`per user` → 按用户
- 持仓状态（console-page.tsx 指标卡）：`{symbol} active` → {symbol} 持仓中
- 委员会页分析师标签（console-page.tsx）：`market agent` / `quant agent` / `macro agent` → 行情分析师 / 量化分析师 / 宏观分析师；`no model trace` → 无模型记录
- 兜底（console-primitives.tsx）：`not recorded` → 未记录；`{n}s ago` → {n} 秒前、`{n}m ago` → {n} 分钟前
- 日期本地化：`formatDate` / `formatRelativeTime` 接收 locale 参数（默认 `en-US` 保持现状），中文页显示 `9月11日 14:30`、`5 分钟前`

## 明确不做

- 不迁移 Tailwind；`globals.css`、布局、视觉不动。
- en-US 文案不动（D 中新增 key 的英文值 = 现有硬编码英文）。
- 后端（含未提交的 `logging.py` 改动）不碰。
- 现有 i18n 测试只断言 `auth.signIn`，不受影响；新增 key 保持中英对称。

## 验证

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run lint
npm run build
```
