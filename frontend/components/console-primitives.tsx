import { Check, CircleAlert, Minus, TrendingDown, TrendingUp } from "lucide-react"

import type { Decision, MarketSnapshot, Position, RiskEvent } from "@/lib/types"

export function VirtualBadge({ compact = false }: { compact?: boolean }) {
  return (
    <span className={`virtual-badge${compact ? " virtual-badge--compact" : ""}`}>
      <span className="status-dot" />
      {compact ? "WEEX virtual" : "WEEX virtual futures · simulation only"}
    </span>
  )
}

export function Metric({ label, value, detail, tone = "neutral" }: {
  label: string
  value: string
  detail?: string
  tone?: "neutral" | "positive" | "negative" | "signal"
}) {
  return (
    <div className={`metric metric--${tone}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      {detail ? <div className="metric-detail">{detail}</div> : null}
    </div>
  )
}

export function RiskBadge({ status }: { status: string }) {
  const normalized = status.toUpperCase()
  const tone = normalized === "ALLOWED" ? "allowed" : normalized === "PAUSED" ? "paused" : "rejected"
  const icon = tone === "allowed" ? <Check size={13} /> : tone === "paused" ? <Minus size={13} /> : <CircleAlert size={13} />
  return <span className={`risk-badge risk-badge--${tone}`}>{icon}{normalized}</span>
}

export function ActionMark({ action }: { action: string }) {
  const normalized = action.toUpperCase()
  if (normalized === "LONG") return <span className="action-mark action-mark--long"><TrendingUp size={15} />LONG</span>
  if (normalized === "SHORT") return <span className="action-mark action-mark--short"><TrendingDown size={15} />SHORT</span>
  return <span className="action-mark action-mark--hold"><Minus size={15} />HOLD</span>
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="empty-state">
      <div className="empty-state-mark">∅</div>
      <div>
        <strong>{title}</strong>
        <p>{body}</p>
      </div>
    </div>
  )
}

export function MarketStrip({ snapshots }: { snapshots: MarketSnapshot[] }) {
  if (!snapshots.length) {
    return <EmptyState title="Waiting for a market snapshot" body="The collector will publish WEEX prices here after the first trading cycle." />
  }
  return (
    <div className="market-strip" aria-label="Recent market snapshots">
      {snapshots.slice(0, 6).map((snapshot) => (
        <div className="market-strip-item" key={`${snapshot.symbol}-${snapshot.captured_at}`}>
          <span>{snapshot.symbol}</span>
          <strong>{formatNumber(snapshot.last_price, 2)}</strong>
          <small>{formatRelativeTime(snapshot.captured_at)}</small>
        </div>
      ))}
    </div>
  )
}

export function DecisionCard({ decision }: { decision: Decision }) {
  const proposal = decision.proposal
  const reasons = decision.risk_decision?.reasons ?? []
  return (
    <article className="decision-card">
      <div className="decision-card-header">
        <div>
          <span className="eyebrow">latest committee call</span>
          <h3><ActionMark action={decision.action} /> <span>{decision.symbol}</span></h3>
        </div>
        <RiskBadge status={decision.status} />
      </div>
      <p className="decision-reasoning">
        {proposal?.reasoning_summary ?? "No trade proposal was emitted. The system remained flat."}
      </p>
      <div className="decision-meta">
        <span>confidence <b>{formatPercent(proposal?.confidence)}</b></span>
        <span>size <b>{formatPercent(proposal?.position_size_pct)}</b></span>
        <span>leverage <b>{proposal?.leverage ?? 1}×</b></span>
      </div>
      {reasons.length ? <div className="risk-reasons">{reasons.join(" · ")}</div> : null}
      <div className="decision-footer">
        <code>{decision.cycle_id.slice(0, 12)}</code>
        <span>{formatDate(decision.created_at)}</span>
      </div>
    </article>
  )
}

export function PortfolioTable({ positions }: { positions: Position[] }) {
  if (!positions.length) return <EmptyState title="No open virtual positions" body="A position appears after the risk gate allows a non-HOLD proposal." />
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Contract</th><th>Side</th><th>Qty</th><th>Entry</th><th>Mark</th><th>uPnL</th></tr></thead>
        <tbody>{positions.map((position) => <tr key={position.id}>
          <td><strong>{position.symbol}</strong><small>{position.status}</small></td>
          <td><ActionMark action={position.side} /></td>
          <td className="mono">{formatNumber(position.quantity, 5)}</td>
          <td className="mono">{formatNumber(position.entry_price, 2)}</td>
          <td className="mono">{position.mark_price == null ? "—" : formatNumber(position.mark_price, 2)}</td>
          <td className={`mono ${position.unrealized_pnl >= 0 ? "positive-text" : "negative-text"}`}>{formatSigned(position.unrealized_pnl, 2)}</td>
        </tr>)}</tbody>
      </table>
    </div>
  )
}

export function EventList({ events }: { events: RiskEvent[] }) {
  if (!events.length) return <EmptyState title="No risk events" body="Risk gate decisions and safety stops will be recorded here." />
  return <div className="event-list">{events.map((event) => <div className="event-row" key={event.id}>
    <div className="event-icon"><CircleAlert size={16} /></div>
    <div><strong>{event.event_type}</strong><p>{event.reason}</p></div>
    <RiskBadge status={event.status} />
    <time>{formatDate(event.created_at)}</time>
  </div>)}</div>
}

export function formatNumber(value: number | null | undefined, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—"
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value)
}

export function formatPercent(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return "—"
  return `${(value * 100).toFixed(1)}%`
}

export function formatSigned(value: number, digits = 2) {
  return `${value >= 0 ? "+" : ""}${formatNumber(value, digits)}`
}

export function formatDate(value: string | number | null | undefined) {
  if (!value) return "not recorded"
  const date = new Date(typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value)
  if (Number.isNaN(date.getTime())) return "not recorded"
  return date.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
}

export function formatRelativeTime(value: string | number) {
  if (typeof value === "string") return formatDate(value)
  const seconds = Math.max(0, Math.floor((Date.now() - (value < 10_000_000_000 ? value * 1000 : value)) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.floor(seconds / 60)}m ago`
}
