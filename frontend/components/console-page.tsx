"use client"

import { useAuth } from "@clerk/nextjs"
import { CirclePause, CirclePlay, RefreshCw, ShieldAlert, Sparkles } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import { ActionMark, DecisionCard, EmptyState, EventList, formatDate, formatNumber, formatPercent, MarketStrip, Metric, PortfolioTable, RiskBadge, VirtualBadge } from "@/components/console-primitives"
import { ApiError, apiRequest } from "@/lib/api"
import type { DashboardView, Decision, ItemsResponse, MarketSnapshot, Position, RiskEvent } from "@/lib/types"

type DashboardData = {
  market: MarketSnapshot[]
  decisions: Decision[]
  positions: Position[]
  events: RiskEvent[]
}

const emptyData: DashboardData = { market: [], decisions: [], positions: [], events: [] }

function useDashboardData() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const [data, setData] = useState<DashboardData>(emptyData)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    if (!isLoaded || !isSignedIn) return
    try {
      const [market, decisions, portfolio, events] = await Promise.all([
        apiRequest<ItemsResponse<MarketSnapshot>>("/market", getToken),
        apiRequest<ItemsResponse<Decision>>("/decisions", getToken),
        apiRequest<ItemsResponse<Position>>("/portfolio", getToken),
        apiRequest<ItemsResponse<RiskEvent>>("/events", getToken),
      ])
      setData({ market: market.items, decisions: decisions.items, positions: portfolio.items, events: events.items })
      setError(null)
      setUpdatedAt(new Date())
    } catch (cause) {
      setError(cause instanceof ApiError ? `API ${cause.status}: ${cause.message}` : "The API could not be reached.")
    }
  }, [getToken, isLoaded, isSignedIn])

  useEffect(() => {
    void refresh()
    const interval = window.setInterval(() => void refresh(), 8_000)
    return () => window.clearInterval(interval)
  }, [refresh])

  return { data, error, updatedAt, refresh }
}

function PageHeader({ title, description, children }: { title: string; description: string; children?: React.ReactNode }) {
  return <header className="page-header">
    <div><div className="page-kicker"><span className="kicker-line" /> WEEX / virtual environment</div><h1>{title}</h1><p>{description}</p></div>
    {children ? <div className="header-actions">{children}</div> : null}
  </header>
}

function ControlPanel({ status, onToggle, busy }: { status: string; onToggle: () => void; busy: boolean }) {
  const paused = status === "PAUSED"
  return <div className="control-panel">
    <div><span className="eyebrow">cycle control</span><strong>{paused ? "Committee paused" : "Committee running"}</strong><small>{paused ? "No automated orders will be created." : "Polling the market every 8 seconds."}</small></div>
    <button className={paused ? "button button--signal" : "button button--quiet"} disabled={busy} onClick={onToggle}>
      {paused ? <CirclePlay size={16} /> : <CirclePause size={16} />}{paused ? "Resume cycles" : "Pause cycles"}
    </button>
  </div>
}

function SyncNote({ error, updatedAt }: { error: string | null; updatedAt: Date | null }) {
  return <div className="sync-note"><span className={error ? "sync-dot sync-dot--error" : "sync-dot"} />{error ? "offline" : `synced ${updatedAt ? formatDate(updatedAt.toISOString()) : "—"}`}</div>
}

export function ConsolePage({ view }: { view: DashboardView }) {
  const { getToken } = useAuth()
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
    if (!window.confirm(`${next === "PAUSED" ? "Pause" : "Resume"} the automated trading cycle?`)) return
    setControlBusy(true)
    try {
      const endpoint = next === "PAUSED" ? "/control/pause" : "/control/resume"
      const response = await apiRequest<{ status: string }>(endpoint, getToken, { method: "POST" })
      setControlStatus(response.status)
      setMessage(next === "PAUSED" ? "Cycle paused. No new virtual orders will be created." : "Cycle resumed.")
    } catch {
      setMessage("Control action failed. The current state was not changed.")
    } finally {
      setControlBusy(false)
    }
  }

  const closePosition = async (symbol: string) => {
    if (!window.confirm(`Close the virtual ${symbol} position? This sends a reduce-only order.`)) return
    try {
      await apiRequest(`/positions/${encodeURIComponent(symbol)}/close`, getToken, { method: "POST" })
      setMessage(`${symbol} close request sent.`)
      await refresh()
    } catch {
      setMessage(`Could not close ${symbol}. No order was confirmed.`)
    }
  }

  if (view === "overview") return <>
    <PageHeader title="The committee is watching." description="A transparent command desk for AI-assisted virtual futures. Every proposal passes through a hard risk gate before it can reach WEEX.">
      <VirtualBadge /><SyncNote error={error} updatedAt={updatedAt} />
    </PageHeader>
    {error ? <div className="alert-banner"><ShieldAlert size={17} /><span>{error} Start the API and refresh to reconnect.</span><button onClick={() => void refresh()}><RefreshCw size={14} />Retry</button></div> : null}
    {message ? <div className="toast" role="status">{message}<button onClick={() => setMessage(null)}>Dismiss</button></div> : null}
    <div className="hero-grid">
      <section className="hero-panel">
        <div className="hero-panel-top"><span className="eyebrow">live decision surface</span><span className="pulse-label"><i /> polling</span></div>
        <div className="hero-copy"><h2>Signal is only<br /><em>useful when bounded.</em></h2><p>Market, quant and macro agents debate in parallel. Risk decides whether the discussion becomes an order.</p></div>
        <div className="flow-line"><span>market</span><b>→</b><span>committee</span><b>→</b><span className="flow-gate">risk gate</span><b>→</b><span>execution</span></div>
      </section>
      <section className="telemetry-panel"><div className="eyebrow">current telemetry</div><div className="telemetry-price">{latestMarket ? `$${formatNumber(latestMarket.last_price, 2)}` : "—"}</div><div className="telemetry-symbol">{latestMarket?.symbol ?? "BTC-USDT"}<span>last price</span></div><div className="telemetry-grid"><span><b>{latestMarket?.funding_rate == null ? "—" : formatPercent(latestMarket.funding_rate)}</b><small>funding</small></span><span><b>{latestMarket?.volume_24h == null ? "—" : formatNumber(latestMarket.volume_24h, 0)}</b><small>24h volume</small></span></div></section>
    </div>
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">at a glance</span><h2>System readout</h2></div><span className="section-index">01 / 04</span></div><div className="metrics-grid"><Metric label="Open virtual positions" value={String(data.positions.length)} detail={data.positions.length ? `${data.positions[0].symbol} active` : "flat book"} /><Metric label="Unrealized PnL" value={formatSignedPnl(pnl)} detail="from synced positions" tone={pnl >= 0 ? "positive" : "negative"} /><Metric label="Last committee action" value={latest ? latest.action : "—"} detail={latest ? formatDate(latest.created_at) : "awaiting first cycle"} tone="signal" /><Metric label="Risk events" value={String(data.events.length)} detail="hard gate history" /></div></section>
    <div className="two-column"><section className="section-block"><div className="section-heading"><div><span className="eyebrow">decision trace</span><h2>What the council decided</h2></div><a href="/committee">View committee <span>↗</span></a></div>{latest ? <DecisionCard decision={latest} /> : <EmptyState title="No decision trace yet" body="Run the worker after the API and WEEX virtual credentials are configured." />}</section><section className="section-block"><div className="section-heading"><div><span className="eyebrow">book state</span><h2>Virtual portfolio</h2></div><a href="/trades">Open ledger <span>↗</span></a></div><PortfolioTable positions={data.positions.slice(0, 3)} /></section></div>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">market feed</span><h2>Recent snapshots</h2></div><span className="section-index">08 sec refresh</span></div><MarketStrip snapshots={data.market} /></section>
  </>

  if (view === "market") return <>
    <PageHeader title="Market pulse" description="The exact market state handed to the agents. Raw facts stay outside the RAG layer so decisions can be audited against the source."><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">WEEX ticker snapshots</span><h2>Price and derivatives context</h2></div><button className="icon-button" onClick={() => void refresh()} aria-label="Refresh market data"><RefreshCw size={16} /></button></div><MarketStrip snapshots={data.market} /><div className="metrics-grid metrics-grid--three"><Metric label="Last price" value={latestMarket ? `$${formatNumber(latestMarket.last_price)}` : "—"} detail={latestMarket?.symbol ?? "waiting"} tone="signal" /><Metric label="Bid / ask" value={latestMarket ? `${formatNumber(latestMarket.bid)} / ${formatNumber(latestMarket.ask)}` : "—"} detail="top of book" /><Metric label="Funding rate" value={latestMarket?.funding_rate == null ? "—" : formatPercent(latestMarket.funding_rate)} detail="from exchange snapshot" /></div></section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">collector contract</span><h2>Data freshness rules</h2></div></div><div className="rule-grid"><div><b>5m / 1h / 4h</b><span>candles supplied to the committee</span></div><div><b>≤ 90 sec</b><span>maximum market age before a proposal is rejected</span></div><div><b>WEEX V3</b><span>virtual endpoint only; no production order path</span></div></div></section>
  </>

  if (view === "committee") return <>
    <PageHeader title="AI committee" description="Three independent lenses, one structured proposal. The model can explain a trade; it cannot bypass the risk engine."><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="committee-banner"><Sparkles size={19} /><div><strong>Model route: deepseek-v4-pro-ga-260813</strong><span>Local retrieval uses BGE-M3 and BGE-Reranker-v2-M3 before the final structured call.</span></div><RiskBadge status={latest?.status ?? "WAITING"} /></section>
    {latest ? <><section className="section-block"><div className="section-heading"><div><span className="eyebrow">last proposal</span><h2><ActionMark action={latest.action} /> {latest.symbol}</h2></div><span className="mono">{latest.cycle_id}</span></div><DecisionCard decision={latest} /></section><section className="agent-grid">{([ ["market agent", latestAnalysis?.market], ["quant agent", latestAnalysis?.quant], ["macro agent", latestAnalysis?.macro] ] as const).map(([label, analysis]) => <article className="agent-card" key={label}><div className="agent-card-top"><span className="agent-glyph"><Sparkles size={14} /></span><span className="eyebrow">{label}</span><b>{analysis?.confidence == null ? "—" : formatPercent(analysis.confidence)}</b></div><p>{analysis?.reasoning_summary ?? "This agent did not return an analysis for the selected cycle."}</p><footer>{analysis?.model_version ?? "no model trace"}</footer></article>)}</section></> : <EmptyState title="The committee has not met yet" body="Once the worker completes a cycle, each agent's reasoning and confidence will be visible here." />}
  </>

  if (view === "trades") return <>
    <PageHeader title="Trade ledger" description="An account-scoped record of virtual positions and committee decisions. Manual close always asks for a second confirmation."><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">open book</span><h2>Positions</h2></div><RiskBadge status="VIRTUAL" /></div><PortfolioTable positions={data.positions} />{data.positions.length ? <div className="close-actions">{data.positions.map((position) => <button className="button button--danger" key={position.id} onClick={() => void closePosition(position.symbol)}>Close {position.symbol}</button>)}</div> : null}</section>
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">decision history</span><h2>Committee calls</h2></div><span className="section-index">{data.decisions.length} records</span></div>{data.decisions.length ? <div className="decision-table">{data.decisions.map((decision) => <div className="decision-row" key={decision.id}><ActionMark action={decision.action} /><span className="decision-row-symbol">{decision.symbol}</span><RiskBadge status={decision.status} /><span className="decision-row-reason">{decision.proposal?.reasoning_summary ?? "No proposal"}</span><time>{formatDate(decision.created_at)}</time></div>)}</div> : <EmptyState title="Trade ledger is empty" body="HOLD decisions will still appear once the first cycle is persisted." />}</section>
  </>

  return <>
    <PageHeader title="Risk events" description="The safety log is intentionally boring. Rejections, pauses and account-data failures are preserved for review."><SyncNote error={error} updatedAt={updatedAt} /></PageHeader>
    <ControlPanel status={controlStatus} onToggle={() => void changeControl()} busy={controlBusy} />
    <section className="section-block"><div className="section-heading"><div><span className="eyebrow">hard gate history</span><h2>What stopped or allowed an order</h2></div><span className="section-index">fail closed</span></div><EventList events={data.events} /></section>
  </>
}

function formatSignedPnl(value: number) {
  return `${value >= 0 ? "+" : ""}${formatNumber(value)}`
}
