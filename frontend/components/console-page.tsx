"use client"

import { useAuth } from "@clerk/nextjs"
import { ChevronRight, CircleAlert, CirclePause, CirclePlay, RefreshCw, ShieldAlert, Sparkles } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import { ActionMark, DecisionCard, EmptyState, EventList, formatDate, formatNumber, formatPercent, MarketStrip, Metric, ModelText, Pagination, PortfolioTable, ReasonText, RiskBadge, VirtualBadge } from "@/components/console-primitives"
import { labelText, modelLabel, reasonLabel, actionLabel } from "@/lib/labels"
import { ApiError, apiRequest } from "@/lib/api"
import { emptyOverviewHint } from "@/lib/console-hints"
import { useI18n } from "@/lib/i18n"
import type { Analysis, DashboardView, Decision, ItemsResponse, MarketSnapshot, PaginatedResponse, Position, RiskEvent, RiskLimits, TradingAccount } from "@/lib/types"

type DashboardData = {
  market: MarketSnapshot[]
  //: 采集周期与新鲜度门由后端配置下发，市场页的「数据时效规则」据此渲染，
  //: 不在前端硬编码（曾写死 5m/1h/4h，而配置早已是 12h/1d）。
  marketTimeframes: string[]
  marketMaxAge: number
  decisions: Decision[]
  decisionsTotal: number
  //: 决策历史里出现过的品种，筛选项由它生成（不硬编码品种列表）。
  decisionSymbols: string[]
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

const emptyData: DashboardData = { market: [], marketTimeframes: [], marketMaxAge: 90, decisions: [], decisionsTotal: 0, decisionSymbols: [], events: [], eventsTotal: 0, positions: [], accounts: [], control: "RUNNING" }

function useDashboardData({
  decisionsPage = 1,
  eventsPage = 1,
  symbol = "",
  action = "",
}: { decisionsPage?: number; eventsPage?: number; symbol?: string; action?: string } = {}) {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const { t } = useI18n()
  const [data, setData] = useState<DashboardData>(emptyData)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    if (!isLoaded || !isSignedIn) return
    try {
      const [market, decisions, portfolio, events, accounts, control] = await Promise.all([
        apiRequest<ItemsResponse<MarketSnapshot> & { timeframes?: string[]; max_age_seconds?: number }>("/market", getToken),
        apiRequest<PaginatedResponse<Decision>>(`/decisions?page=${decisionsPage}&page_size=${PAGE_SIZE}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}${action ? `&action=${action}` : ""}`, getToken),
        apiRequest<ItemsResponse<Position>>("/portfolio", getToken),
        apiRequest<PaginatedResponse<RiskEvent>>(`/events?page=${eventsPage}&page_size=${PAGE_SIZE}`, getToken),
        apiRequest<ItemsResponse<TradingAccount>>("/accounts", getToken),
        apiRequest<{ status: string }>("/control/status", getToken),
      ])
      setData({ market: market.items, marketTimeframes: market.timeframes ?? [], marketMaxAge: market.max_age_seconds ?? 90, decisions: decisions.items, decisionsTotal: decisions.total, decisionSymbols: decisions.symbols ?? [], events: events.items, eventsTotal: events.total, positions: portfolio.items, accounts: accounts.items, control: control.status })
      setError(null)
      setUpdatedAt(new Date())
    } catch (cause) {
      // 401 是凭证问题，不是 API 挂了：让用户去重启 API 只会白折腾，轮询还在跑，会自己恢复。
      if (cause instanceof ApiError) {
        const hint = cause.status === 401 ? t("console.reauthenticating") : t("console.reconnect")
        setError(`${t("console.apiError", { status: cause.status, message: cause.message })} ${hint}`)
      } else {
        setError(`${t("console.apiUnavailable")} ${t("console.reconnect")}`)
      }
    }
  }, [getToken, isLoaded, isSignedIn, t, decisionsPage, eventsPage, symbol, action])

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
  // 机器码 → 界面文案。后端存的是稳定的英文码（审计要用的标识），
  // 在这里按当前语言翻译，见 lib/labels.ts。
  const reasonLabelText = (code: string) => labelText(reasonLabel(code), t)
  const separator = t("common.listSeparator")
  const isEntry = proposal != null && proposal.action !== "HOLD"
  const reasoningText = proposal?.reasoning_summary ? reasonLabelText(proposal.reasoning_summary) : ""
  const invalidationText = (proposal?.invalidation_conditions ?? []).map(reasonLabelText).join(separator)

  const agents: Array<[string, Analysis]> = []
  if (analyses?.market) agents.push(["committee.agentMarket", analyses.market])
  if (analyses?.quant) agents.push(["committee.agentQuant", analyses.quant])
  if (analyses?.macro) agents.push(["committee.agentMacro", analyses.macro])

  return <div className="decision-row-wrap">
    <button type="button" className="decision-row" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <ActionMark action={decision.action} />
      <span className="decision-row-symbol">{decision.symbol}</span>
      <RiskBadge status={decision.status} />
      <span className="decision-row-reason"><ReasonText code={proposal?.reasoning_summary} fallback={t("common.noProposal")} /></span>
      <time>{formatDate(decision.created_at, locale)}</time>
      <ChevronRight size={14} className={open ? "decision-row-chevron is-open" : "decision-row-chevron"} />
    </button>
    {open ? <div className="decision-detail">
      {proposal ? <section>
        <h4>{t("trades.detailProposal")}</h4>
        <div className="decision-detail-grid">
          <span>{t("common.confidence")} <b>{formatPercent(proposal.confidence)}</b></span>
          {proposal.model_version ? <span>{t("trades.modelVersion")} <b>{labelText(modelLabel(proposal.model_version), t)}</b></span> : null}
          {/* 观望单没有仓位、杠杆、止损止盈、有效期可言。把它们显示出来只会是
              「仓位 0.0% / 杠杆 20×」这种噪声，反而掩盖了真正的原因。 */}
          {isEntry ? <>
            <span>{t("common.size")} <b>{formatPercent(proposal.position_size_pct)}</b></span>
            <span title={t("console.leverageFixed")}>{t("common.leverage")} <b>{decision.leverage ?? proposal.leverage ?? 1}×</b></span>
            {proposal.stop_loss != null ? <span>{t("trades.stopLoss")} <b>{proposal.stop_loss}</b></span> : null}
            {proposal.take_profit != null ? <span>{t("trades.takeProfit")} <b>{proposal.take_profit}</b></span> : null}
            {proposal.valid_until ? <span>{t("trades.validUntil")} <b>{formatDate(proposal.valid_until, locale)}</b></span> : null}
          </> : null}
        </div>
        {/* 规则信号器把同一句话同时写进 reasoning_summary 和 invalidation_conditions，
            照原样渲染会一字不差地重复两遍。 */}
        {invalidationText && invalidationText !== reasoningText ? <p className="decision-detail-line">{t("trades.invalidation")}: {invalidationText}</p> : null}
        {proposal.evidence_refs?.length ? <p className="decision-detail-line">{t("trades.evidence")}: {proposal.evidence_refs.join(separator)}</p> : null}
        {reasoningText ? <p className="decision-detail-reasoning">{reasoningText}</p> : null}
      </section> : null}
      {agents.length ? <section>
        <h4>{t("trades.detailAnalyses")}</h4>
        <div className="agent-grid">
          {agents.map(([labelKey, analysis]) => <article className="agent-card" key={labelKey}>
            <div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{t(labelKey)}</span><b>{formatPercent(analysis.confidence)}</b></div>
            <p><ReasonText code={analysis.reasoning_summary} /></p>
            <footer><ModelText version={analysis.model_version} fallback={t("committee.noModelTrace")} /></footer>
          </article>)}
        </div>
      </section> : null}
      {risk ? <section>
        <h4>{t("trades.detailRisk")}</h4>
        <div className="decision-detail-grid">
          <span>{t("trades.execStatus")} <b><RiskBadge status={risk.status ?? "UNKNOWN"} /></b></span>
          {risk.reasons?.length ? <span>{t("trades.reasons")} <b>{risk.reasons.map((reason) => labelText(reasonLabel(reason), t)).join(separator)}</b></span> : null}
          {risk.adjusted_position_size_pct != null ? <span>{t("trades.adjustedSize")} <b>{formatPercent(risk.adjusted_position_size_pct)}</b></span> : null}
        </div>
      </section> : null}
      {execution ? <section>
        <h4>{t("trades.detailExecution")}</h4>
        <div className="decision-detail-grid">
          <span>{t("trades.execStatus")} <b>{execution.status}</b></span>
          {/* 成交均价与回合盈亏：后端一直在记，但之前从不显示 —— 于是「这笔平仓
              到底赚没赚」在界面上根本看不到。 */}
          {execution.average_price != null ? <span>{t("trades.avgPrice")} <b>{formatNumber(Number(execution.average_price), 2)}</b></span> : null}
          {execution.realized_pnl != null ? <span>{t("trades.realizedPnl")} <b className={Number(execution.realized_pnl) >= 0 ? "positive-text" : "negative-text"}>{formatSignedPnl(Number(execution.realized_pnl))}</b></span> : null}
          {execution.client_order_id ? <span>{t("trades.clientOrderId")} <b>{execution.client_order_id}</b></span> : null}
          {execution.exchange_order_id ? <span>{t("trades.orderId")} <b>{execution.exchange_order_id}</b></span> : null}
          {execution.message ? <span>{t("trades.message")} <b>{execution.message}</b></span> : null}
        </div>
      </section> : null}
      {/* 裸 UUID 对交易者没有意义，但排查时要用 —— 给个标签，完整值放 title。 */}
      <div className="decision-detail-footer">{t("trades.cycleId")} <code title={decision.cycle_id}>{decision.cycle_id?.slice(0, 8)}</code></div>
    </div> : null}
  </div>
}

export function ConsolePage({ view }: { view: DashboardView }) {
  const { getToken } = useAuth()
  const { t, locale } = useI18n()
  // 各列表分页只在对应视图生效；其他视图固定第 1 页（要「最近」数据）。
  const [decisionsPage, setDecisionsPage] = useState(1)
  const [eventsPage, setEventsPage] = useState(1)
  // 品种多起来之后不筛就看不过来。换筛选要回到第 1 页 —— 否则会停在
  // 一个过滤后不存在的页码上，显示空白。
  const [symbolFilter, setSymbolFilter] = useState("")
  const [actionFilter, setActionFilter] = useState("")
  const changeFilter = (apply: () => void) => {
    apply()
    setDecisionsPage(1)
  }
  const { data, error, updatedAt, refresh } = useDashboardData({
    decisionsPage: view === "trades" ? decisionsPage : 1,
    eventsPage: view === "events" ? eventsPage : 1,
    symbol: view === "trades" ? symbolFilter : "",
    action: view === "trades" ? actionFilter : "",
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
  // 平仓按钮只属于真正 OPEN 的仓位。后端已过滤掉已平仓的行，这里再挡一道：
  // 拿着一个已平仓的 symbol 去下平仓单，平的是不存在的仓位。
  const openPositions = useMemo(() => data.positions.filter((item) => item.status === "OPEN"), [data.positions])
  const pnl = useMemo(() => openPositions.reduce((total, item) => total + item.unrealized_pnl, 0), [openPositions])

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
    {error ? <div className="alert-banner"><ShieldAlert size={17} /><span>{error}</span><button onClick={() => void refresh()}><RefreshCw size={14} />{t("console.retry")}</button></div> : null}
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
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.atAGlance")}</span><h2>{t("overview.systemReadout")}</h2></div><span className="section-index">01 / 04</span></div><div className="metrics-grid"><Metric label={t("overview.openPositions")} value={String(openPositions.length)} detail={openPositions.length ? t("overview.positionActive", { symbol: openPositions[0].symbol }) : t("overview.flatBook")} /><Metric label={t("overview.unrealizedPnl")} value={formatSignedPnl(pnl)} detail={t("overview.syncedPositions")} tone={pnl >= 0 ? "positive" : "negative"} /><Metric label={t("overview.lastAction")} value={latest ? labelText(actionLabel(latest.action), t) : "—"} detail={latest ? formatDate(latest.created_at, locale) : t("overview.awaitingCycle")} tone="signal" /><Metric label={t("overview.riskEvents")} value={String(data.eventsTotal)} detail={t("overview.hardGateHistory")} /></div></section>
    <div className="two-column"><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.decisionTrace")}</span><h2>{t("overview.whatDecided")}</h2></div><a href="/committee">{t("overview.viewCommittee")} <span>↗</span></a></div>{latest ? <DecisionCard decision={latest} /> : <EmptyState title={t("overview.noDecision")} body={t("overview.noDecisionBody")} />}</section><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.bookState")}</span><h2>{t("overview.virtualPortfolio")}</h2></div><a href="/trades">{t("overview.openLedger")} <span>↗</span></a></div><PortfolioTable positions={data.positions.slice(0, 3)} /></section></div>
  </>

  if (view === "market") return <>
    <PageHeader title={t("market.title")} description={t("market.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("market.snapshots")}</span><h2>{t("market.context")}</h2></div><button className="icon-button" onClick={() => void refresh()} aria-label={t("market.refresh")}><RefreshCw size={16} /></button></div><MarketStrip snapshots={data.market} /><div className="metrics-grid metrics-grid--three"><Metric label={t("overview.lastPrice")} value={latestMarket ? `$${formatNumber(latestMarket.last_price)}` : "—"} detail={latestMarket?.symbol ?? t("common.waiting")} tone="signal" /><Metric label={t("market.bidAsk")} value={latestMarket ? `${formatNumber(latestMarket.bid)} / ${formatNumber(latestMarket.ask)}` : "—"} detail={t("market.topOfBook")} /><Metric label={t("overview.funding")} value={latestMarket?.funding_rate == null ? "—" : formatPercent(latestMarket.funding_rate)} detail={t("market.fromExchange")} /></div></section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("market.contract")}</span><h2>{t("market.freshness")}</h2></div></div><div className="rule-grid"><div><b>{data.marketTimeframes.join(" / ") || "—"}</b><span>{t("market.candles")}</span></div><div><b>≤ {data.marketMaxAge} sec</b><span>{t("market.maxAge")}</span></div><div><b>WEEX V3</b><span>{t("market.endpoint")}</span></div></div></section>
  </>

  if (view === "committee") return <>
    <PageHeader title={t("committee.title")} description={t("committee.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="committee-banner"><Sparkles size={19} /><div><strong>{t("committee.modelRoute")}</strong><span>{t("committee.retrieval")}</span></div><RiskBadge status={latest?.status ?? "WAITING"} /></section>
    {latest ? <><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("committee.lastProposal")}</span><h2><ActionMark action={latest.action} /> {latest.symbol}</h2></div><span className="mono">{latest.cycle_id}</span></div><DecisionCard decision={latest} /></section><section className="agent-grid">{([ ["committee.agentMarket", latestAnalysis?.market], ["committee.agentQuant", latestAnalysis?.quant], ["committee.agentMacro", latestAnalysis?.macro] ] as const).map(([labelKey, analysis]) => <article className="agent-card" key={labelKey}><div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{t(labelKey)}</span><b>{analysis?.confidence == null ? "—" : formatPercent(analysis.confidence)}</b></div><p><ReasonText code={analysis?.reasoning_summary} fallback={t("committee.noMeetingBody")} /></p><footer><ModelText version={analysis?.model_version} fallback={t("committee.noModelTrace")} /></footer></article>)}</section></> : <EmptyState title={t("committee.noMeeting")} body={t("committee.noMeetingBody")} />}
  </>

  if (view === "settings") return <>
    <PageHeader title={t("settings.title")} description={t("settings.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    {message ? <div className="toast" role="status">{message}<button onClick={() => setMessage(null)}>{t("console.dismiss")}</button></div> : null}
    <AccountCard accounts={data.accounts} refresh={refresh} />
  </>

  if (view === "trades") return <>
    <PageHeader title={t("trades.title")} description={t("trades.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.openBook")}</span><h2>{t("trades.positions")}</h2></div><RiskBadge status="VIRTUAL" /></div><PortfolioTable positions={data.positions} onClose={closePosition} /></section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.history")}</span><h2>{t("trades.calls")}</h2></div><span className="section-index">{t("trades.records", { count: data.decisionsTotal })}</span></div><div className="filter-row">
      <label>{t("trades.filterSymbol")}
        <select value={symbolFilter} onChange={(event) => changeFilter(() => setSymbolFilter(event.target.value))}>
          <option value="">{t("trades.filterAll")}</option>
          {data.decisionSymbols.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>{t("trades.filterAction")}
        <select value={actionFilter} onChange={(event) => changeFilter(() => setActionFilter(event.target.value))}>
          <option value="">{t("trades.filterAll")}</option>
          {["HOLD", "LONG", "SHORT", "CLOSE"].map((item) => <option key={item} value={item}>{labelText(actionLabel(item), t)}</option>)}
        </select>
      </label>
      {symbolFilter || actionFilter ? <button type="button" className="button button--quiet" onClick={() => changeFilter(() => { setSymbolFilter(""); setActionFilter("") })}>{t("trades.filterReset")}</button> : null}
    </div>{data.decisions.length ? <div className="decision-table">{data.decisions.map((decision) => <DecisionRow key={decision.id} decision={decision} />)}</div> : <EmptyState title={t("trades.empty")} body={t("trades.emptyBody")} />}<Pagination page={decisionsPage} total={data.decisionsTotal} pageSize={PAGE_SIZE} onChange={setDecisionsPage} /></section>
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
