"use client"

import { useAuth } from "@clerk/nextjs"
import { CirclePause, CirclePlay, RefreshCw, ShieldAlert, Sparkles } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import { ActionMark, DecisionCard, EmptyState, EventList, formatDate, formatNumber, formatPercent, MarketStrip, Metric, PortfolioTable, RiskBadge, VirtualBadge } from "@/components/console-primitives"
import { ApiError, apiRequest } from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import type { DashboardView, Decision, ItemsResponse, MarketSnapshot, Position, RiskEvent, TradingAccount } from "@/lib/types"

type DashboardData = {
  market: MarketSnapshot[]
  decisions: Decision[]
  positions: Position[]
  events: RiskEvent[]
  accounts: TradingAccount[]
}

const emptyData: DashboardData = { market: [], decisions: [], positions: [], events: [], accounts: [] }

function useDashboardData() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const { t } = useI18n()
  const [data, setData] = useState<DashboardData>(emptyData)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    if (!isLoaded || !isSignedIn) return
    try {
      const [market, decisions, portfolio, events, accounts] = await Promise.all([
        apiRequest<ItemsResponse<MarketSnapshot>>("/market", getToken),
        apiRequest<ItemsResponse<Decision>>("/decisions", getToken),
        apiRequest<ItemsResponse<Position>>("/portfolio", getToken),
        apiRequest<ItemsResponse<RiskEvent>>("/events", getToken),
        apiRequest<ItemsResponse<TradingAccount>>("/accounts", getToken),
      ])
      setData({ market: market.items, decisions: decisions.items, positions: portfolio.items, events: events.items, accounts: accounts.items })
      setError(null)
      setUpdatedAt(new Date())
    } catch (cause) {
      setError(cause instanceof ApiError ? t("console.apiError", { status: cause.status, message: cause.message }) : t("console.apiUnavailable"))
    }
  }, [getToken, isLoaded, isSignedIn, t])

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
  const { t } = useI18n()
  return <div className="sync-note"><span className={error ? "sync-dot sync-dot--error" : "sync-dot"} />{error ? t("console.offline") : t("console.synced", { value: updatedAt ? formatDate(updatedAt.toISOString()) : "—" })}</div>
}

function AccountCard({ accounts, refresh }: { accounts: TradingAccount[]; refresh: () => Promise<void> }) {
  const { getToken } = useAuth()
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState("")
  const [apiSecret, setApiSecret] = useState("")
  const [passphrase, setPassphrase] = useState("")

  const createAccount = async () => {
    setBusy(true)
    try {
      await apiRequest("/accounts", getToken, {
        method: "POST",
        body: JSON.stringify({
          api_key_ref: apiKey || "pending",
          api_secret_ref: apiSecret || "pending",
          passphrase_ref: passphrase || "pending",
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

  return <section className="section-block">
    <div className="section-heading">
      <div><span className="eyebrow">trading account</span><h2>WEEX virtual account</h2></div>
      <span className="section-index">per user</span>
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
          </div>
        ))}
      </div>
    ) : (
      <p className="account-hint">{t("console.noTradingAccount")}</p>
    )}
    {accounts.length ? null : (
      <div className="account-form">
        <input value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={t("console.apiKey")} autoComplete="off" />
        <input value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} placeholder={t("console.apiSecret")} autoComplete="off" />
        <input value={passphrase} onChange={(event) => setPassphrase(event.target.value)} placeholder={t("console.passphrase")} autoComplete="off" />
        <button className="button button--signal" disabled={busy} onClick={() => void createAccount()}>{t("console.createAccount")}</button>
      </div>
    )}
    {message ? <p className="account-hint" role="status">{message}</p> : null}
  </section>
}

export function ConsolePage({ view }: { view: DashboardView }) {
  const { getToken } = useAuth()
  const { t } = useI18n()
  const { data, error, updatedAt, refresh } = useDashboardData()
  const [controlStatus, setControlStatus] = useState("RUNNING")
  const [controlBusy, setControlBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const latest = data.decisions[0]
  const latestMarket = data.market[0]
  const latestAnalysis = latest?.analyses
  const pnl = useMemo(() => data.positions.reduce((total, item) => total + item.unrealized_pnl, 0), [data.positions])

  const changeControl = async () => {
    const next = controlStatus === "PAUSED" ? "RUNNING" : "PAUSED"
    if (!window.confirm(next === "PAUSED" ? t("console.pauseConfirm") : t("console.resumeConfirm"))) return
    setControlBusy(true)
    try {
      const endpoint = next === "PAUSED" ? "/control/pause" : "/control/resume"
      const response = await apiRequest<{ status: string }>(endpoint, getToken, { method: "POST" })
      setControlStatus(response.status)
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
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <AccountCard accounts={data.accounts} refresh={refresh} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("overview.atAGlance")}</span><h2>{t("overview.systemReadout")}</h2></div><span className="section-index">01 / 04</span></div><div className="metrics-grid"><Metric label={t("overview.openPositions")} value={String(data.positions.length)} detail={data.positions.length ? `${data.positions[0].symbol} active` : t("overview.flatBook")} /><Metric label={t("overview.unrealizedPnl")} value={formatSignedPnl(pnl)} detail={t("overview.syncedPositions")} tone={pnl >= 0 ? "positive" : "negative"} /><Metric label={t("overview.lastAction")} value={latest ? latest.action : "—"} detail={latest ? formatDate(latest.created_at) : t("overview.awaitingCycle")} tone="signal" /><Metric label={t("overview.riskEvents")} value={String(data.events.length)} detail={t("overview.hardGateHistory")} /></div></section>
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
    {latest ? <><section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("committee.lastProposal")}</span><h2><ActionMark action={latest.action} /> {latest.symbol}</h2></div><span className="mono">{latest.cycle_id}</span></div><DecisionCard decision={latest} /></section><section className="agent-grid">{([ ["market agent", latestAnalysis?.market], ["quant agent", latestAnalysis?.quant], ["macro agent", latestAnalysis?.macro] ] as const).map(([label, analysis]) => <article className="agent-card" key={label}><div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{label}</span><b>{analysis?.confidence == null ? "—" : formatPercent(analysis.confidence)}</b></div><p>{analysis?.reasoning_summary ?? t("committee.noMeetingBody")}</p><footer>{analysis?.model_version ?? "no model trace"}</footer></article>)}</section></> : <EmptyState title={t("committee.noMeeting")} body={t("committee.noMeetingBody")} />}
  </>

  if (view === "trades") return <>
    <PageHeader title={t("trades.title")} description={t("trades.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.openBook")}</span><h2>{t("trades.positions")}</h2></div><RiskBadge status="VIRTUAL" /></div><PortfolioTable positions={data.positions} />{data.positions.length ? <div className="close-actions">{data.positions.map((position) => <button className="button button--danger" key={position.id} onClick={() => void closePosition(position.symbol)}>{t("trades.close", { symbol: position.symbol })}</button>)}</div> : null}</section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("trades.history")}</span><h2>{t("trades.calls")}</h2></div><span className="section-index">{t("trades.records", { count: data.decisions.length })}</span></div>{data.decisions.length ? <div className="decision-table">{data.decisions.map((decision) => <div className="decision-row" key={decision.id}><ActionMark action={decision.action} /><span className="decision-row-symbol">{decision.symbol}</span><RiskBadge status={decision.status} /><span className="decision-row-reason">{decision.proposal?.reasoning_summary ?? t("common.noProposal")}</span><time>{formatDate(decision.created_at)}</time></div>)}</div> : <EmptyState title={t("trades.empty")} body={t("trades.emptyBody")} />}</section>
  </>

  return <>
    <PageHeader title={t("events.title")} description={t("events.description")}><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">{t("events.history")}</span><h2>{t("events.stopped")}</h2></div><span className="section-index">{t("events.failClosed")}</span></div><EventList events={data.events} /></section>
  </>
}

function formatSignedPnl(value: number) {
  return `${value >= 0 ? "+" : ""}${formatNumber(value)}`
}
