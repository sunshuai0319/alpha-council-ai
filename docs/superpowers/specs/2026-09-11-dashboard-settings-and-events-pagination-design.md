# Dashboard 瘦身 + 账户设置页 + events 分页

## Context

overview 页面当前自上而下有 6 块内容：页头、周期控制、WEEX 虚拟账户（凭证 +
启用/停用 + 风控上限表单）、系统状态指标、最近决策 + 持仓、市场快照。其中账户
块是"配置"性质却常驻总览，占了大半屏，导致需要向下滚动过多才能看到真正的状态
信息；市场快照与已有的 `/market` 页重复。

同时 `events` 页面把全部风控事件一次性渲染，没有分页（`trades` 刚加了分页）。

目标：把配置类内容移出总览到独立设置页，去掉与专用页重复的行情块，并给 events
补上与 trades 一致的分页。

## 设计

### 1. events 分页（复用 trades 模式）

- 后端 `GET /api/events` 加 `page` / `page_size`（默认 20，`page_size` 上限 100），
  响应体从 `{items}` 变为 `{items, total, page, page_size}` —— 与 `/decisions` 完全同模式。
- `TradingCycleService.events` 加同名参数，用 `func.count()` 取总数、`offset`/`limit` 取当页。
- 前端抽出共用 `Pagination` 组件（把 trades 现在内联的那段搬进 `console-primitives.tsx`），
  trades 与 events 共用。
- 每页条数 20，与 `DECISIONS_PAGE_SIZE` 一致。
- events 页其余布局（页头、周期控制、事件列表本身）不变，只在列表下方加分页控件。

### 2. 新建设置页 `/settings`

- 新路由 `frontend/app/(console)/settings/page.tsx`，内容 `<ConsolePage view="settings" />`
  （沿用现有每个路由一个 view 的模式）。
- `DashboardView` 类型加 `"settings"`。
- 把现有 `AccountCard` 整体搬到 settings 视图：凭证展示（默认脱敏 + 展开明文）、
  启用/停用、风控上限表单、无账户时的创建表单，行为不变。
- 页头用 i18n 文案（新增 `settings.title` / `settings.description`）。

### 3. sidebar 左下角入口

- 在 `dashboard-shell.tsx` 的 `sidebar-bottom` → `account-row` 增加一个带齿轮图标的
  「WEEX 账户」链接，指向 `/settings`，与语言切换、Clerk 用户按钮并排。

### 4. overview 瘦身

- **移除**：账户卡片（迁到设置页）、市场快照 `MarketStrip`（`/market` 页已有更完整行情）。
- **保留**：页头、周期控制、系统状态指标、最近决策 + 持仓。
- overview 从 6 块降到 4 块；`emptyOverviewHint` 的数据提示逻辑保持不变。

### 5. 数据请求

- `useDashboardData` 签名从位置参数改为对象 `{ decisionsPage, eventsPage }`，各自按视图
  传自己的页码：trades 传 `decisionsPage`，events 传 `eventsPage`，其他视图固定第 1 页
  （保证「最近决策」始终最新）。
- `/events` 请求同样带 `page` / `page_size`。

### 6. 测试

- 后端：`/events` 分页测试（仿 `test_decisions_are_paginated_newest_first`）；更新
  `test_routes.py` 里对 `/api/events` 响应体的断言。
- 前端：events 分页交互（点下一页后请求带 `page=2`）；settings 视图渲染账户卡；
  overview 不再渲染账户卡的断言。

## 涉及文件

**后端**
- `backend/app/services/cycle.py` — `events()` 加分页参数
- `backend/app/api/routes/dashboard.py` — `/events` 加 query 参数
- `backend/tests/api/test_cycle_service.py`、`backend/tests/api/test_routes.py`

**前端**
- `frontend/components/console-page.tsx` — 设置视图、overview 移除两块、`useDashboardData` 签名
- `frontend/components/console-primitives.tsx` — 抽 `Pagination` 组件
- `frontend/components/dashboard-shell.tsx` — sidebar 设置入口
- `frontend/app/(console)/settings/page.tsx` — 新路由
- `frontend/lib/types.ts` — `DashboardView` 加 `"settings"`
- `frontend/lib/i18n.tsx` — 设置页与入口文案
- `frontend/app/globals.css` — 设置入口/分页样式微调
- `frontend/tests/control-panel.test.tsx`

## 验证

- `cd backend && .venv/bin/python -m pytest tests/ -q` 全绿
- `cd frontend && npx vitest run && npx tsc --noEmit` 全绿
- 手动：重启后端 → 打开 `/settings` 能看到账户卡并操作；`/dashboard` 不再有账户块和
  市场快照；`/events`、`/trades` 底部有分页且翻页请求带 `page`。
