"use client"

import { useAuth } from "@clerk/nextjs"
import { ChevronRight, CircleAlert, CirclePause, CirclePlay, RefreshCw, ShieldAlert, Sparkles } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import { ActionMark, DecisionCard, EmptyState, EventList, formatDate, formatNumber, formatPercent, MarketStrip, Metric, Pagination, PortfolioTable, RiskBadge, VirtualBadge } from "@/components/console-primitives"
import { ApiError, apiRequest } from "@/lib/api"
import { emptyOverviewHint } from "@/lib/console-hints"
import { useI18n } from "@/lib/i18n"
import type { Analysis, DashboardView, Decision, ItemsResponse, MarketSnapshot, PaginatedResponse, Position, RiskEvent, RiskLimits, TradingAccount } from "@/lib/types"

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

// 凭证默认脱敏：只露首尾，中间打点。短值（如 passphrase）整串打点。
function maskCredential(value: string) {
  if (value.length <= 8) return "•".repeat(value.length)
  return `${value.slice(0, 6)}${"•".repeat(8)}${value.slice(-4)}`
}

const emptyData: DashboardData = { market: [], decisions: [], decisionsTotal: 0, events: [], eventsTotal: 0, positions: [], accounts: [], control: "RUNNING" }

function useDashboardData({ decisionsPage = 1, eventsPage = 1 }: { decisionsPage?: number; eventsPage?: number } = {}) {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const { t } = useI18n()
  const [data, setData] = useState<DashboardData>(emptyData)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    if (!isLoaded || !isSignedIn) return
    try {
      const [market, decisions, portfolio, events, accounts, control] = await Promise.all([
        apiRequest<ItemsResponse<MarketSnapshot>>("/market", getToken),
        apiRequest<PaginatedResponse<Decision>>(`/decisions?page=${decisionsPage}&page_size=${PAGE_SIZE}`, getToken),
        apiRequest<ItemsResponse<Position>>("/portfolio", getToken),
        apiRequest<PaginatedResponse<RiskEvent>>(`/events?page=${eventsPage}&page_size=${PAGE_SIZE}`, getToken),
        apiRequest<ItemsResponse<TradingAccount>>("/accounts", getToken),
        apiRequest<{ status: string }>("/control/status", getToken),
      ])
      setData({ market: market.items, decisions: decisions.items, decisionsTotal: decisions.total, events: events.items, eventsTotal: events.total, positions: portfolio.items, accounts: accounts.items, control: control.status })
      setError(null)
      setUpdatedAt(new Date())
    } catch (cause) {
      setError(cause instanceof ApiError ? t("console.apiError", { status: cause.status, message: cause.message }) : t("console.apiUnavailable"))
    }
  }, [getToken, isLoaded, isSignedIn, t, decisionsPage, eventsPage])

  useEffect(() => {
    void refresh()
    const interval = window.setInterval(() => void refresh(), 8_000)
    return () => window.clearInterval(interval)
  }, [refresh])

  return { data, error, updatedAt, refresh }
}

function PageHeader({ title, description, children }: { title: string; description: string; children?: React.ReactNode }) {
  const { t } = useI18n()
  return <header className="page-header">
    <div><div className="page-kicker"><span className="kicker-line" /> {t("console.virtualEnvironment")}</div><h1>{title}</h1><p>{description}</p></div>
    {children ? <div className="header-actions">{children}</div> : null}
  </header>
}

function ControlPanel({ status, onToggle, busy }: { status: string; onToggle: () => void; busy: boolean }) {
  const { t } = useI18n()
  const paused = status === "PAUSED"
  return <div className="control-panel">
    <div><span className="eyebrow">{t("console.cycleControl")}</span><strong>{paused ? t("console.committeePaused") : t("console.committeeRunning")}</strong><small>{paused ? t("console.noAutomatedOrders") : t("console.polling")}</small></div>
    <button className={paused ? "button button--signal" : "button button--quiet"} disabled={busy} onClick={onToggle}>
      {paused ? <CirclePlay size={16} /> : <CirclePause size={16} />}{paused ? t("console.resumeCycles") : t("console.pauseCycles")}
    </button>
  </div>
}

function SyncNote({ error, updatedAt }: { error: string | null; updatedAt: Date | null }) {
  const { t, locale } = useI18n()
  return <div className="sync-note"><span className={error ? "sync-dot sync-dot--error" : "sync-dot"} />{error ? t("console.offline") : t("console.synced", { value: updatedAt ? formatDate(updatedAt.toISOString(), locale) : "—" })}</div>
}

const PERCENT_FIELDS = [
  { key: "max_position_notional_pct", label: "console.maxPositionNotionalPct" },
  { key: "max_single_trade_risk_pct", label: "console.maxSingleTradeRiskPct" },
  { key: "max_daily_loss_pct", label: "console.maxDailyLossPct" },
] as const

/** 百分比在后端存小数（0.05），UI 用百分数（5）更好输入。 */
const toPercent = (value: number) => String(Number((value * 100).toFixed(4)))

function RiskLimitsForm({ account, onSave, busy }: {
  account: TradingAccount
  onSave: (limits: RiskLimits) => Promise<void>
  busy: boolean
}) {
  const { t } = useI18n()
  const effective = account.effective_risk_limits
  const platform = account.platform_limits
  const [draft, setDraft] = useState<Record<string, string>>({})

  const current = PERCENT_FIELDS.map((field) => ({
    ...field,
    value: draft[field.key] ?? toPercent(account.risk_limits?.[field.key] ?? platform?.[field.key] ?? 0),
    cap: platform?.[field.key],
  }))
  const losses = {
    value: draft.max_consecutive_losses ?? String(account.risk_limits?.max_consecutive_losses ?? platform?.max_consecutive_losses ?? ""),
    cap: platform?.max_consecutive_losses,
  }

  const submit = () => {
    const limits: RiskLimits = {}
    for (const field of PERCENT_FIELDS) {
      const raw = draft[field.key]
      if (raw !== undefined) limits[field.key] = Number(raw) / 100
    }
    if (draft.max_consecutive_losses !== undefined) {
      limits.max_consecutive_losses = Number(draft.max_consecutive_losses)
    }
    return onSave(limits)
  }

  const dirty = Object.keys(draft).length > 0

  return <div className="account-risk">
    <div className="account-risk-head">
      <span className="eyebrow">{t("console.riskLimits")}</span>
      <small>{t("console.riskLimitsHint")}</small>
    </div>
    <div className="account-risk-grid">
      {current.map((field) => (
        <label key={field.key}>
          <span>{t(field.label)}</span>
          <input
            type="number"
            min={0}
            max={field.cap}
            step="0.1"
            // 默认值来自平台上限，用户改了才提交，避免一进来就把默认值写成偏好
            value={field.value}
            onChange={(event) => setDraft({ ...draft, [field.key]: event.target.value })}
          />
        </label>
      ))}
      <label>
        <span>{t("console.maxConsecutiveLosses")}</span>
        <input
          type="number"
          min={1}
          max={losses.cap}
          step={1}
          value={losses.value}
          onChange={(event) => setDraft({ ...draft, max_consecutive_losses: event.target.value })}
        />
      </label>
      {/* 虚拟盘杠杆改不了，所以只读展示；给输入框会让人以为能改 */}
      <label>
        <span>{t("console.leverage")}</span>
        <input
          type="text"
          readOnly
          value={effective ? `${effective.max_leverage}x` : "—"}
          title={t("console.leverageFixed")}
        />
      </label>
    </div>
    <button className="button button--quiet" disabled={busy || !dirty} onClick={() => void submit()}>
      {t("console.saveRiskLimits")}
    </button>
  </div>
}

function AccountCard({ accounts, refresh }: { accounts: TradingAccount[]; refresh: () => Promise<void> }) {
  const { getToken } = useAuth()
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState("")
  const [apiSecret, setApiSecret] = useState("")
  const [passphrase, setPassphrase] = useState("")
  const [revealed, setRevealed] = useState<Record<string, boolean>>({})

  const createAccount = async () => {
    setBusy(true)
    try {
      await apiRequest("/accounts", getToken, {
        method: "POST",
        body: JSON.stringify({
          api_key_ref: apiKey.trim(),
          api_secret_ref: apiSecret.trim(),
          passphrase_ref: passphrase.trim(),
          environment: "virtual",
          provider: "weex",
        }),
      })
      setMessage(t("console.accountCreated"))
      setApiKey("")
      setApiSecret("")
      setPassphrase("")
      await refresh()
    } catch {
      setMessage(t("console.accountCreateFailed"))
    } finally {
      setBusy(false)
    }
  }

  const toggleEnabled = async (account: TradingAccount) => {
    setBusy(true)
    try {
      await apiRequest(`/accounts/${account.id}`, getToken, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !account.enabled }),
      })
      setMessage(account.enabled ? t("console.accountDisabled") : t("console.accountEnabled"))
      await refresh()
    } catch {
      setMessage(t("console.accountUpdateFailed"))
    } finally {
      setBusy(false)
    }
  }

  const saveRiskLimits = async (account: TradingAccount, limits: RiskLimits) => {
    setBusy(true)
    try {
      await apiRequest(`/accounts/${account.id}`, getToken, {
        method: "PATCH",
        // enabled 必须带上：这是整体更新，漏掉会把它重置
        body: JSON.stringify({ enabled: account.enabled, risk_limits: limits }),
      })
      setMessage(t("console.riskLimitsSaved"))
      await refresh()
    } catch {
      setMessage(t("console.riskLimitsFailed"))
    } finally {
      setBusy(false)
    }
  }

  return <section className="section-block">
    <div className="section-heading">
      <div><span className="eyebrow">{t("console.tradingAccount")}</span><h2>{t("console.weexVirtualAccount")}</h2></div>
      <span className="section-index">{t("console.perUser")}</span>
    </div>
    {accounts.length ? (
      <div className="account-list">
        {accounts.map((account) => (
          <div className="account-row-card" key={account.id}>
            <div className="account-meta"><b>{account.provider} · {account.environment}</b><span>{account.configured ? t("console.apiKey") : t("console.noCredentials")}</span></div>
            <div className="account-row-actions">
              <span className="account-status">{account.enabled ? <b>{t("common.enabled")}</b> : t("common.disabled")}</span>
              <button className={account.enabled ? "button button--quiet" : "button button--signal"} disabled={busy} onClick={() => void toggleEnabled(account)}>
                {account.enabled ? t("console.disable") : t("console.enable")}
              </button>
            </div>
            {account.configured && account.credentials ? (
              <div className="account-credentials">
                <div className="credential-row"><span>{t("console.apiKey")}</span><code>{revealed[account.id] ? account.credentials.api_key : maskCredential(account.credentials.api_key)}</code></div>
                <div className="credential-row"><span>{t("console.apiSecret")}</span><code>{revealed[account.id] ? account.credentials.api_secret : maskCredential(account.credentials.api_secret)}</code></div>
                <div className="credential-row"><span>{t("console.passphrase")}</span><code>{revealed[account.id] ? account.credentials.passphrase : maskCredential(account.credentials.passphrase)}</code></div>
                <button type="button" className="credential-toggle" onClick={() => setRevealed((prev) => ({ ...prev, [account.id]: !prev[account.id] }))}>
                  {revealed[account.id] ? t("console.hideCredentials") : t("console.showCredentials")}
                </button>
              </div>
            ) : null}
            <RiskLimitsForm
              account={account}
              busy={busy}
              onSave={(limits) => saveRiskLimits(account, limits)}
            />
          </div>
        ))}
      </div>
    ) : (
      <p className="account-hint">{t("console.noTradingAccount")}</p>
    )}
    {accounts.length ? null : (
      <div className="account-form">
        <input required value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={t("console.apiKey")} autoComplete="off" />
        <input required value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} placeholder={t("console.apiSecret")} autoComplete="off" />
        <input required value={passphrase} onChange={(event) => setPassphrase(event.target.value)} placeholder={t("console.passphrase")} autoComplete="off" />
        <button className="button button--signal" disabled={busy || !apiKey.trim() || !apiSecret.trim() || !passphrase.trim()} onClick={() => void createAccount()}>{t("console.createAccount")}</button>
      </div>
    )}
    {message ? <p className="account-hint" role="status">{message}</p> : null}
  </section>
}

function DecisionRow({ decision }: { decision: Decision }) {
  const { t, locale } = useI18n()
  const [open, setOpen] = useState(false)
  const proposal = decision.proposal
  const risk = decision.risk_decision
  const execution = decision.execution_result
  const analyses = decision.analyses
  const agents: Array<[string, Analysis]> = []
  if (analyses?.market) agents.push(["committee.agentMarket", analyses.market])
  if (analyses?.quant) agents.push(["committee.agentQuant", analyses.quant])
  if (analyses?.macro) agents.push(["committee.agentMacro", analyses.macro])

  return <div className="decision-row-wrap">
    <button type="button" className="decision-row" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <ActionMark action={decision.action} />
      <span className="decision-row-symbol">{decision.symbol}</span>
      <RiskBadge status={decision.status} />
      <span className="decision-row-reason">{proposal?.reasoning_summary ?? t("common.noProposal")}</span>
      <time>{formatDate(decision.created_at, locale)}</time>
      <ChevronRight size={14} className={open ? "decision-row-chevron is-open" : "decision-row-chevron"} />
    </button>
    {open ? <div className="decision-detail">
      {proposal ? <section>
        <h4>{t("trades.detailProposal")}</h4>
        <div className="decision-detail-grid">
          <span>{t("common.confidence")} <b>{formatPercent(proposal.confidence)}</b></span>
          <span>{t("common.size")} <b>{formatPercent(proposal.position_size_pct)}</b></span>
          <span title={t("console.leverageFixed")}>{t("common.leverage")} <b>{decision.leverage ?? proposal.leverage ?? 1}×</b></span>
          {proposal.stop_loss != null ? <span>{t("trades.stopLoss")} <b>{proposal.stop_loss}</b></span> : null}
          {proposal.take_profit != null ? <span>{t("trades.takeProfit")} <b>{proposal.take_profit}</b></span> : null}
          {proposal.valid_until ? <span>{t("trades.validUntil")} <b>{formatDate(proposal.valid_until, locale)}</b></span> : null}
          {proposal.model_version ? <span>{t("trades.modelVersion")} <b>{proposal.model_version}</b></span> : null}
        </div>
        {proposal.invalidation_conditions?.length ? <p className="decision-detail-line">{t("trades.invalidation")}: {proposal.invalidation_conditions.join("; ")}</p> : null}
        {proposal.evidence_refs?.length ? <p className="decision-detail-line">{t("trades.evidence")}: {proposal.evidence_refs.join(", ")}</p> : null}
        <p className="decision-detail-reasoning">{proposal.reasoning_summary}</p>
      </section> : null}
      {agents.length ? <section>
        <h4>{t("trades.detailAnalyses")}</h4>
        <div className="agent-grid">
          {agents.map(([labelKey, analysis]) => <article className="agent-card" key={labelKey}>
            <div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{t(labelKey)}</span><b>{formatPercent(analysis.confidence)}</b></div>
            <p>{analysis.reasoning_summary ?? "—"}</p>
            <footer>{analysis.model_version ?? t("committee.noModelTrace")}</footer>
          </article>)}
        </div>
      </section> : null}
      {risk ? <section>
        <h4>{t("trades.detailRisk")}</h4>
        <div className="decision-detail-grid">
          <span>{t("trades.execStatus")} <b><RiskBadge status={risk.status ?? "UNKNOWN"} /></b></span>
          {risk.reasons?.length ? <span>{t("trades.reasons")} <b>{risk.reasons.join("; ")}</b></span> : null}
          {risk.adjusted_position_size_pct != null ? <span>{t("trades.adjustedSize")} <b>{formatPercent(risk.adjusted_position_size_pct)}</b></span> : null}
        </div>
      </section> : null}
      {execution ? <section>
        <h4>{t("trades.detailExecution")}</h4>
        <div className="decision-detail-grid">
          <span>{t("trades.execStatus")} <b>{execution.status}</b></span>
          {execution.client_order_id ? <span>{t("trades.clientOrderId")} <b>{execution.client_order_id}</b></span> : null}
          {execution.exchange_order_id ? <span>{t("trades.orderId")} <b>{execution.exchange_order_id}</b></span> : null}
          {execution.message ? <span>{t("trades.message")} <b>{execution.message}</b></span> : null}
        </div>
      </section> : null}
      <div className="decision-detail-footer"><code>{decision.cycle_id}</code></div>
    </div> : null}
  </div>
}

export function ConsolePage({ view }: { view: DashboardView }) {
  const { getToken } = useAuth()
  const { t, locale } = useI18n()
  // 各列表分页只在对应视图生效；其他视图固定第 1 页（要「最近」数据）。
  const [decisionsPage, setDecisionsPage] = useState(1)
  const [eventsPage, setEventsPage] = useState(1)
  const { data, error, updatedAt, refresh } = useDashboardData({
    decisionsPage: view === "trades" ? decisionsPage : 1,
    eventsPage: view === "events" ? eventsPage : 1,
  })
  const [controlBusy, setControlBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  // 状态来自服务端：熔断会自动暂停账户，本地默认值会让用户误以为还在交易。
  const controlStatus = data.control
  // 操作提示 5 秒后自动消失，不用手动关；按钮仍在，错误消息也能快速关掉。
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(null), 5000)
    return () => clearTimeout(timer)
  }, [message])
  // 语言偏好同步到后端：worker 按它决定 LLM 生成的分析文本用中文还是英文。
  useEffect(() => {
    void apiRequest("/preferences/locale", getToken, {
      method: "PUT",
      body: JSON.stringify({ locale }),
    }).catch(() => undefined)
  }, [locale, getToken])

  const latest = data.decisions[0]
  const latestMarket = data.market[0]
  const dataHint = emptyOverviewHint(data.accounts, data.market)
  const latestAnalysis = latest?.analyses
  const pnl = useMemo(() => data.positions.reduce((total, item) => total + item.unrealized_pnl, 0), [data.positions])

  const changeControl = async () => {
    const next = controlStatus === "PAUSED" ? "RUNNING" : "PAUSED"
    if (!window.confirm(next === "PAUSED" ? t("console.pauseConfirm") : t("console.resumeConfirm"))) return
    setControlBusy(true)
    try {
      const endpoint = next === "PAUSED" ? "/control/pause" : "/control/resume"
      await apiRequest<{ status: string }>(endpoint, getToken, { method: "POST" })
      await refresh()
      setMessage(next === "PAUSED" ? t("console.pausedMessage") : t("console.resumedMessage"))
    } catch {
      setMessage(t("console.controlFailed"))
    } finally {
      setControlBusy(false)
    }
  }

  const closePosition = async (symbol: string) => {
    if (!window.confirm(t("console.closeConfirm", { symbol }))) return
    try {
      await apiRequest(`/positions/${encodeURIComponent(symbol)}/close`, getToken, { method: "POST" })
      setMessage(t("console.closeSent", { symbol }))
      await refresh()
    } catch {
      setMessage(t("console.closeFailed", { symbol }))
    }
  }

  if (view === "overview") return <>
    <PageHeader title={t("overview.title")} description={t("overview.description")}>
      <VirtualBadge /><SyncNote error={error} updatedAt={updatedAt} />
    </PageHeader>
    {error ? <div className="alert-banner"><ShieldAlert size={17} /><span>{error} {t("console.reconnect")}</span><button onClick={() => void refresh()}><RefreshCw size={14} />{t("console.retry")}</button></div> : null}
    {message ? <div className="toast" role="status">{message}<button onClick={() => setMessage(null)}>{t("console.dismiss")}</button></div> : null}
    <div className="hero-grid">
      <section className="hero-panel">
        <div className="hero-panel-top"><span className="eyebrow">{t("overview.liveSurface")}</span><span className="pulse-label"><i /> {t("overview.polling")}</span></div>
        <div className="hero-copy"><h2 dangerouslySetInnerHTML={{ __html: t("overview.heroTitle") }} /><p>{t("overview.heroBody")}</p></div>
        <div className="flow-line"><span>{t("overview.market")}</span><b>→</b><span>{t("overview.committee")}</span><b>→</b><span className="flow-gate">{t("overview.riskGate")}</span><b>→</b><span>{t("overview.execution")}</span></div>
      </section>
      <section className="telemetry-panel"><div className="eyebrow">{t("overview.telemetry")}</div><div className="telemetry-price">{latestMarket ? `$${formatNumber(latestMarket.last_price, 2)}` : "—"}</div><div className="telemetry-symbol">{latestMarket?.symbol ?? "BTC-USDT"}<span>{t("overview.lastPrice")}</span></div><div className="telemetry-grid"><span><b>{latestMarket?.funding_rate == null ? "—" : formatPercent(latestMarket.funding_rate)}</b><small>{t("overview.funding")}</small></span><span><b>{latestMarket?.volume_24h == null ? "—" : formatNumber(latestMarket.volume_24h, 0)}</b><small>{t("overview.volume")}</small></span></div></section>
    </div>
    {/* 只显示「—」会让用户以为界面坏了：说明为什么没有数据，以及该做什么 */}
    {dataHint ? <p className="data-hint" role="status"><CircleAlert size={15} /><span>{t(dataHint)}</span></p> : null}
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <AccountCard accounts={data.accounts} refresh={refresh} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.atAGlance")}</span><h2>{t("overview.systemReadout")}</h2></div><span className="section-index">01 / 04</span></div><div className="metrics-grid"><Metric label={t("overview.openPositions")} value={String(data.positions.length)} detail={data.positions.length ? t("overview.positionActive", { symbol: data.positions[0].symbol }) : t("overview.flatBook")} /><Metric label={t("overview.unrealizedPnl")} value={formatSignedPnl(pnl)} detail={t("overview.syncedPositions")} tone={pnl >= 0 ? "positive" : "negative"} /><Metric label={t("overview.lastAction")} value={latest ? latest.action : "—"} detail={latest ? formatDate(latest.created_at, locale) : t("overview.awaitingCycle")} tone="signal" /><Metric label={t("overview.riskEvents")} value={String(data.events.length)} detail={t("overview.hardGateHistory")} /></div></section>
    <div className="two-column"><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.decisionTrace")}</span><h2>{t("overview.whatDecided")}</h2></div><a href="/committee">{t("overview.viewCommittee")} <span>↗</span></a></div>{latest ? <DecisionCard decision={latest} /> : <EmptyState title={t("overview.noDecision")} body={t("overview.noDecisionBody")} />}</section><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.bookState")}</span><h2>{t("overview.virtualPortfolio")}</h2></div><a href="/trades">{t("overview.openLedger")} <span>↗</span></a></div><PortfolioTable positions={data.positions.slice(0, 3)} /></section></div>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.marketFeed")}</span><h2>{t("overview.recentSnapshots")}</h2></div><span className="section-index">{t("overview.refresh")}</span></div><MarketStrip snapshots={data.market} /></section>
  </>

  if (view === "market") return <>
    <PageHeader title={t("market.title")} description={t("market.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("market.snapshots")}</span><h2>{t("market.context")}</h2></div><button className="icon-button" onClick={() => void refresh()} aria-label={t("market.refresh")}><RefreshCw size={16} /></button></div><MarketStrip snapshots={data.market} /><div className="metrics-grid metrics-grid--three"><Metric label={t("overview.lastPrice")} value={latestMarket ? `$${formatNumber(latestMarket.last_price)}` : "—"} detail={latestMarket?.symbol ?? t("common.waiting")} tone="signal" /><Metric label={t("market.bidAsk")} value={latestMarket ? `${formatNumber(latestMarket.bid)} / ${formatNumber(latestMarket.ask)}` : "—"} detail={t("market.topOfBook")} /><Metric label={t("overview.funding")} value={latestMarket?.funding_rate == null ? "—" : formatPercent(latestMarket.funding_rate)} detail={t("market.fromExchange")} /></div></section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("market.contract")}</span><h2>{t("market.freshness")}</h2></div></div><div className="rule-grid"><div><b>5m / 1h / 4h</b><span>{t("market.candles")}</span></div><div><b>≤ 90 sec</b><span>{t("market.maxAge")}</span></div><div><b>WEEX V3</b><span>{t("market.endpoint")}</span></div></div></section>
  </>

  if (view === "committee") return <>
    <PageHeader title={t("committee.title")} description={t("committee.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="committee-banner"><Sparkles size={19} /><div><strong>{t("committee.modelRoute")}</strong><span>{t("committee.retrieval")}</span></div><RiskBadge status={latest?.status ?? "WAITING"} /></section>
    {latest ? <><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("committee.lastProposal")}</span><h2><ActionMark action={latest.action} /> {latest.symbol}</h2></div><span className="mono">{latest.cycle_id}</span></div><DecisionCard decision={latest} /></section><section className="agent-grid">{([ ["committee.agentMarket", latestAnalysis?.market], ["committee.agentQuant", latestAnalysis?.quant], ["committee.agentMacro", latestAnalysis?.macro] ] as const).map(([labelKey, analysis]) => <article className="agent-card" key={labelKey}><div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{t(labelKey)}</span><b>{analysis?.confidence == null ? "—" : formatPercent(analysis.confidence)}</b></div><p>{analysis?.reasoning_summary ?? t("committee.noMeetingBody")}</p><footer>{analysis?.model_version ?? t("committee.noModelTrace")}</footer></article>)}</section></> : <EmptyState title={t("committee.noMeeting")} body={t("committee.noMeetingBody")} />}
  </>

  if (view === "trades") return <>
    <PageHeader title={t("trades.title")} description={t("trades.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.openBook")}</span><h2>{t("trades.positions")}</h2></div><RiskBadge status="VIRTUAL" /></div><PortfolioTable positions={data.positions} />{data.positions.length ? <div className="close-actions">{data.positions.map((position) => <button className="button button--danger" key={position.id} onClick={() => void closePosition(position.symbol)}>{t("trades.close", { symbol: position.symbol })}</button>)}</div> : null}</section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.history")}</span><h2>{t("trades.calls")}</h2></div><span className="section-index">{t("trades.records", { count: data.decisionsTotal })}</span></div>{data.decisions.length ? <div className="decision-table">{data.decisions.map((decision) => <DecisionRow key={decision.id} decision={decision} />)}</div> : <EmptyState title={t("trades.empty")} body={t("trades.emptyBody")} />}<Pagination page={decisionsPage} total={data.decisionsTotal} pageSize={PAGE_SIZE} onChange={setDecisionsPage} /></section>
  </>

  return <>
    <PageHeader title={t("events.title")} description={t("events.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("events.history")}</span><h2>{t("events.stopped")}</h2></div><span className="section-index">{t("events.failClosed")}</span></div><EventList events={data.events} /><Pagination page={eventsPage} total={data.eventsTotal} pageSize={PAGE_SIZE} onChange={setEventsPage} /></section>
  </>
}

function formatSignedPnl(value: number) {
  return `${value >= 0 ? "+" : ""}${formatNumber(value)}`
}
