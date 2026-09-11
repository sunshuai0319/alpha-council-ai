# Dashboard 设置页 + events 分页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 WEEX 账户配置移出 overview 到独立设置页、移除 overview 中与 /market 重复的行情块、给 events 列表补上与 trades 一致的分页。

**Architecture:** 后端 `/events` 复用 `/decisions` 已落地的分页模式（`page`/`page_size` + `total`）；前端把 trades 内联的分页控件抽成 `Pagination` 组件供两个列表共用，新增 `/settings` 路由复用现有「每路由一个 view」模式，`useDashboardData` 改为接收各列表的页码。

**Tech Stack:** FastAPI + SQLAlchemy（后端）；Next.js App Router + React + vitest（前端）。

---

### Task 1: 后端 `/events` 分页

**Files:**
- Modify: `backend/app/services/cycle.py:794-814`（`events` 方法）
- Modify: `backend/app/api/routes/dashboard.py:54-59`（`events` 路由）
- Test: `backend/tests/api/test_cycle_service.py`（追加测试）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/api/test_cycle_service.py` 末尾追加（`RiskEvent`、`RiskStatus`、`datetime`、`UTC` 该文件已 import）：

```python
def test_events_are_paginated_newest_first(tmp_path) -> None:
    """风控事件列表要分页，否则事件多了会一次全拉回来。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'events-paging.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        base = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        for index in range(5):
            db.add(
                RiskEvent(
                    id=f"e-{index}",
                    user_id="u-1",
                    event_type="RISK_GATE",
                    status=RiskStatus.ALLOWED,
                    reason=f"r-{index}",
                    created_at=base.replace(minute=index),
                )
            )
        db.commit()
        service = TradingCycleService(db=db)

        first = service.events("u-1", page=1, page_size=2)
        assert first["total"] == 5
        assert [item["id"] for item in first["items"]] == ["e-4", "e-3"]

        last = service.events("u-1", page=3, page_size=2)
        assert [item["id"] for item in last["items"]] == ["e-0"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_cycle_service.py::test_events_are_paginated_newest_first -q`
Expected: FAIL —— `TypeError: events() got an unexpected keyword argument 'page'`

- [ ] **Step 3: 改 service**

把 `backend/app/services/cycle.py` 的 `events` 方法整体替换为：

```python
    def events(self, user_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        if self.db is not None:
            total = self.db.scalar(
                select(func.count())
                .select_from(RiskEvent)
                .where(RiskEvent.user_id == user_id)
            ) or 0
            rows = self.db.scalars(
                select(RiskEvent)
                .where(RiskEvent.user_id == user_id)
                .order_by(RiskEvent.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [
                    {
                        "id": row.id,
                        "event_type": row.event_type,
                        "status": row.status.value if hasattr(row.status, "value") else row.status,
                        "reason": row.reason,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
```

- [ ] **Step 4: 改路由**

把 `backend/app/api/routes/dashboard.py` 的 events 路由替换为：

```python
@router.get("/events")
def events(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.events(user.id, page=page, page_size=page_size)
```

- [ ] **Step 5: 修正受影响的既有断言**

`backend/tests/api/test_routes.py` 里若有对 `/api/events` 响应体的断言，更新为含 `total`/`page`/`page_size`。在 `test_dashboard_routes_use_authenticated_user_scope` 的 portfolio 断言后加一行：

```python
        events = TestClient(app).get("/api/events").json()
        assert events == {"items": [], "total": 0, "page": 1, "page_size": 20}
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/cycle.py backend/app/api/routes/dashboard.py backend/tests/api/test_cycle_service.py backend/tests/api/test_routes.py
git commit -m "feat: paginate the risk event list"
```

---

### Task 2: 抽出共用 `Pagination` 组件，trades 改用它

**Files:**
- Modify: `frontend/components/console-primitives.tsx`（新增导出组件）
- Modify: `frontend/components/console-page.tsx:465-469`（trades 删内联分页）
- Test: `frontend/tests/control-panel.test.tsx`（既有分页测试须继续通过）

- [ ] **Step 1: 在 `console-primitives.tsx` 末尾新增组件**

```tsx
export function Pagination({
  page,
  total,
  pageSize,
  onChange,
}: {
  page: number
  total: number
  pageSize: number
  onChange: (page: number) => void
}) {
  const { t } = useI18n()
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  // 只有多于一页才需要控件；单页时整个隐藏，避免噪音。
  if (total <= pageSize) return null
  return (
    <div className="pagination">
      <button type="button" className="button button--quiet" disabled={page <= 1} onClick={() => onChange(Math.max(1, page - 1))}>{t("common.prevPage")}</button>
      <span>{t("common.pageOf", { page, total: totalPages })}</span>
      <button type="button" className="button button--quiet" disabled={page >= totalPages} onClick={() => onChange(page + 1)}>{t("common.nextPage")}</button>
    </div>
  )
}
```

该文件已 `import { useI18n } from "@/lib/i18n"`（`MarketStrip` 等在用），无需新增 import。

- [ ] **Step 2: trades 视图改用它**

在 `console-page.tsx` 的 import 行里给 `@/components/console-primitives` 的具名导入加 `Pagination`。然后把 trades 列表那条 `<section>`（`console-page.tsx:465-469`）替换为：

```tsx
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.history")}</span><h2>{t("trades.calls")}</h2></div><span className="section-index">{t("trades.records", { count: data.decisionsTotal })}</span></div>{data.decisions.length ? <div className="decision-table">{data.decisions.map((decision) => <DecisionRow key={decision.id} decision={decision} />)}</div> : <EmptyState title={t("trades.empty")} body={t("trades.emptyBody")} />}<Pagination page={decisionsPage} total={data.decisionsTotal} pageSize={DECISIONS_PAGE_SIZE} onChange={setDecisionsPage} /></section>
```

- [ ] **Step 3: 跑前端测试确认仍通过**

Run: `cd frontend && npx vitest run tests/control-panel.test.tsx && npx tsc --noEmit`
Expected: 既有「paginates the trade ledger」等测试 PASS，typecheck 0 错误

- [ ] **Step 4: 提交**

```bash
git add frontend/components/console-primitives.tsx frontend/components/console-page.tsx
git commit -m "refactor: extract a shared Pagination component"
```

---

### Task 3: 前端 events 分页

**Files:**
- Modify: `frontend/components/console-page.tsx`（`useDashboardData`、`DashboardData`、ConsolePage state、events 视图）
- Test: `frontend/tests/control-panel.test.tsx`

- [ ] **Step 1: 写失败测试**

在 `frontend/tests/control-panel.test.tsx` 的 `describe` 内追加：

```tsx
  it("paginates the risk event list", async () => {
    stubApi("RUNNING")

    render(<ConsolePage view="events" />)

    await vi.waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      expect(calls.some(([url]) => String(url).includes("/events?page=1"))).toBe(true)
    })
  })
```

需要先让 `stubApi` 的 `/events` 返回分页体：把 `stubApi` 里 `"/events": { items: [] }` 改成 `"/events": { items: [], total: 0, page: 1, page_size: 20 }`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run tests/control-panel.test.tsx -t "paginates the risk event list"`
Expected: FAIL —— 请求 URL 里没有 `page=1`

- [ ] **Step 3: 改 `useDashboardData` 与数据形状**

在 `console-page.tsx`：

1. `DashboardData` 加 `eventsTotal: number`：

```tsx
type DashboardData = {
  market: MarketSnapshot[]
  decisions: Decision[]
  decisionsTotal: number
  events: RiskEvent[]
  eventsTotal: number
  positions: Position[]
  accounts: TradingAccount[]
  control: string
}

//: 与后端列表接口默认每页条数一致；分页控件按它算总页数。
const PAGE_SIZE = 20
```

（删除原来的 `DECISIONS_PAGE_SIZE`，把 Task 2 里 trades 的 `DECISIONS_PAGE_SIZE` 一并替换为 `PAGE_SIZE`。）

2. `emptyData` 加 `eventsTotal: 0`。

3. `useDashboardData` 改对象参数：

```tsx
function useDashboardData({ decisionsPage = 1, eventsPage = 1 }: { decisionsPage?: number; eventsPage?: number } = {}) {
```

4. `refresh` 里 `/events` 请求改为 `apiRequest<PaginatedResponse<RiskEvent>>(\`/events?page=${eventsPage}&page_size=${PAGE_SIZE}\`, getToken)`，`/decisions` 的 `DECISIONS_PAGE_SIZE` 换成 `PAGE_SIZE`，`setData` 加 `eventsTotal: events.total`，`useCallback` 依赖加 `eventsPage`。

- [ ] **Step 4: ConsolePage 加 events 页码 state 并传参**

```tsx
  const [decisionsPage, setDecisionsPage] = useState(1)
  const [eventsPage, setEventsPage] = useState(1)
  const { data, error, updatedAt, refresh } = useDashboardData({
    decisionsPage: view === "trades" ? decisionsPage : 1,
    eventsPage: view === "events" ? eventsPage : 1,
  })
```

- [ ] **Step 5: events 视图加 `Pagination`**

把 events 那条 `<section>`（末尾 return 里）替换为：

```tsx
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("events.history")}</span><h2>{t("events.stopped")}</h2></div><span className="section-index">{t("events.failClosed")}</span></div><EventList events={data.events} /><Pagination page={eventsPage} total={data.eventsTotal} pageSize={PAGE_SIZE} onChange={setEventsPage} /></section>
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: 全部 PASS，typecheck 0 错误

- [ ] **Step 7: 提交**

```bash
git add frontend/components/console-page.tsx frontend/tests/control-panel.test.tsx
git commit -m "feat: paginate the risk event list in the console"
```

---

### Task 4: 设置页与 settings 视图

**Files:**
- Modify: `frontend/lib/types.ts`（`DashboardView`）
- Create: `frontend/app/(console)/settings/page.tsx`
- Modify: `frontend/components/console-page.tsx`（settings 视图）
- Modify: `frontend/lib/i18n.tsx`（文案）
- Test: `frontend/tests/control-panel.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
  it("renders the account card on the settings view", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="settings" />)

    expect(await screen.findByText("WEEX 虚拟账户")).toBeVisible()
    expect(screen.getByText("启用")).toBeVisible()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run tests/control-panel.test.tsx -t "settings view"`
Expected: FAIL —— 找不到「WEEX 虚拟账户」（settings 视图尚不存在）

- [ ] **Step 3: 扩展 `DashboardView` 类型**

`frontend/lib/types.ts` 里 `DashboardView` 加 `"settings"`（保持既有联合类型写法）。

- [ ] **Step 4: 新建路由**

`frontend/app/(console)/settings/page.tsx`：

```tsx
import { ConsolePage } from "@/components/console-page"

export default function SettingsPage() {
  return <ConsolePage view="settings" />
}
```

- [ ] **Step 5: 加 settings 视图**

在 `console-page.tsx` 里，`if (view === "trades")` 分支之前加：

```tsx
  if (view === "settings") return <>
    <PageHeader title={t("settings.title")} description={t("settings.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    {message ? <div className="toast" role="status">{message}<button onClick={() => setMessage(null)}>{t("console.dismiss")}</button></div> : null}
    <AccountCard accounts={data.accounts} refresh={refresh} />
  </>
```

- [ ] **Step 6: 加 i18n 文案**

`frontend/lib/i18n.tsx` 的 zh 段（`console.*` 附近）加：

```tsx
    "settings.title": "账户设置",
    "settings.description": "配置用于自动交易的 WEEX 虚拟账户与风控上限。",
```

en 段加：

```tsx
    "settings.title": "Account settings",
    "settings.description": "Configure the WEEX virtual account and risk limits used for automated trading.",
```

- [ ] **Step 7: 跑测试确认通过**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add frontend/lib/types.ts "frontend/app/(console)/settings/page.tsx" frontend/components/console-page.tsx frontend/lib/i18n.tsx frontend/tests/control-panel.test.tsx
git commit -m "feat: move the WEEX account card to a settings page"
```

---

### Task 5: overview 瘦身

**Files:**
- Modify: `frontend/components/console-page.tsx:427-448`（overview 视图）
- Test: `frontend/tests/control-panel.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
  it("keeps configuration off the overview", async () => {
    stubApi("RUNNING", [virtualAccount])

    render(<ConsolePage view="overview" />)

    await screen.findByText("AI 智囊团值守中。")
    expect(screen.queryByText("WEEX 虚拟账户")).toBeNull()
    expect(screen.queryByText("最近快照")).toBeNull()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run tests/control-panel.test.tsx -t "keeps configuration off the overview"`
Expected: FAIL —— overview 仍渲染账户卡与市场快照

- [ ] **Step 3: 从 overview 移除两块**

在 `console-page.tsx` 的 overview 视图里删除这两行（`AccountCard` 与市场快照 `<section>`）：

```tsx
    <AccountCard accounts={data.accounts} refresh={refresh} />
```

```tsx
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.marketFeed")}</span><h2>{t("overview.recentSnapshots")}</h2></div><span className="section-index">{t("overview.refresh")}</span></div><MarketStrip snapshots={data.market} /></section>
```

`AccountCard` 函数本身保留（Task 4 的 settings 视图在用）。`MarketStrip` 在 market 视图仍被使用，import 保留。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/components/console-page.tsx frontend/tests/control-panel.test.tsx
git commit -m "refactor: trim the overview to status, decisions and positions"
```

---

### Task 6: sidebar 左下角设置入口

**Files:**
- Modify: `frontend/components/dashboard-shell.tsx`（`account-row`）
- Modify: `frontend/app/globals.css`（入口样式）
- Modify: `frontend/lib/i18n.tsx`（入口文案）
- Test: `frontend/tests/control-panel.test.tsx`（可选：入口链接存在）

- [ ] **Step 1: 在 sidebar-bottom 加链接**

`frontend/components/dashboard-shell.tsx` 顶部 import 加 `Settings` 图标（`lucide-react`），并把 `account-row` 那行替换为：

```tsx
          <div className="account-row"><Link className="account-settings" href="/settings" aria-label={t("nav.accountSettings")} title={t("nav.accountSettings")}><Settings size={14} /></Link><LanguageSwitcher />{clerkEnabled ? <UserButton afterSignOutUrl="/" /> : <span className="account-placeholder">●</span>}<span>{t("nav.account")}</span></div>
```

- [ ] **Step 2: 加文案**

`frontend/lib/i18n.tsx` zh 段（`nav.*` 附近）加 `"nav.accountSettings": "WEEX 账户设置",`；en 段加 `"nav.accountSettings": "WEEX account settings",`。

- [ ] **Step 3: 加入口样式**

`frontend/app/globals.css` 的 `.account-row` 规则后追加：

```css
.account-settings { display: grid; place-items: center; width: 28px; height: 28px; color: var(--muted); border: 1px solid var(--line); border-radius: 6px; }
.account-settings:hover { color: var(--paper); border-color: #687f7e; }
```

- [ ] **Step 4: 跑检查**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`
Expected: typecheck 0 错误，测试全绿

- [ ] **Step 5: 提交**

```bash
git add frontend/components/dashboard-shell.tsx frontend/app/globals.css frontend/lib/i18n.tsx
git commit -m "feat: add a WEEX account settings entry in the sidebar"
```

---

## 验证（全部完成后）

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
cd ../frontend && npx vitest run && npx tsc --noEmit
```

手动：重启后端 → `/settings` 可操作账户卡；`/dashboard` 无账户块与市场快照；`/events`、`/trades` 列表底部有分页且请求带 `page`。
