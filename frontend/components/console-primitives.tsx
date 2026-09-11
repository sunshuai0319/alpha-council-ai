import { Check, CircleAlert, Minus, TrendingDown, TrendingUp, X } from "lucide-react"

import { translate, useI18n, type Locale } from "@/lib/i18n"
import { labelText, modelLabel, eventTypeLabel, reasonLabel, reasonParts } from "@/lib/labels"
import type { Decision, MarketSnapshot, Position, RiskEvent } from "@/lib/types"

/**
 * 后端机器码 → 当前语言文案。
 *
 * 单点转换：折叠行、决策卡片、委员会页、展开详情全都用它。之前只有详情做了
 * 转换，于是列表行仍在显示 `signal_hold_score_0.18` 这种英文码。
 * 认不出的码（包括 LLM 写的中文摘要）原样返回。
 */
export function ReasonText({ code, fallback = "—" }: { code?: string | null; fallback?: string }) {
  const { t } = useI18n()
  if (!code) return <>{fallback}</>
  return <>{labelText(reasonLabel(code), t)}</>
}

/** 模型版本 → 当前语言文案。 */
export function ModelText({ version, fallback = "—" }: { version?: string | null; fallback?: string }) {
  const { t } = useI18n()
  if (!version) return <>{fallback}</>
  return <>{labelText(modelLabel(version), t)}</>
}

export function VirtualBadge({ compact = false }: { compact?: boolean }) {
  const { t } = useI18n()
  return (
    <span className={`virtual-badge${compact ? " virtual-badge--compact" : ""}`}>
      <span className="status-dot" />
      {compact ? "WEEX virtual" : t("nav.simulation")}
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
  const { t } = useI18n()
  const normalized = status.toUpperCase()
  const tone = normalized === "ALLOWED" ? "allowed" : normalized === "PAUSED" ? "paused" : normalized === "WAITING" || normalized === "VIRTUAL" ? "neutral" : "rejected"
  const icon = tone === "allowed" ? <Check size={13} /> : tone === "paused" || tone === "neutral" ? <Minus size={13} /> : <CircleAlert size={13} />
  return <span className={`risk-badge risk-badge--${tone}`}>{icon}{normalized === "WAITING" ? t("common.waiting") : normalized}</span>
}

export function ActionMark({ action }: { action: string }) {
  const { t } = useI18n()
  const normalized = action.toUpperCase()
  if (normalized === "LONG") return <span className="action-mark action-mark--long"><TrendingUp size={15} />{t("action.long")}</span>
  if (normalized === "SHORT") return <span className="action-mark action-mark--short"><TrendingDown size={15} />{t("action.short")}</span>
  if (normalized === "CLOSE") return <span className="action-mark action-mark--close"><X size={15} />{t("action.close")}</span>
  return <span className="action-mark action-mark--hold"><Minus size={15} />{t("action.hold")}</span>
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
  const { t, locale } = useI18n()
  if (!snapshots.length) {
    return <EmptyState title={t("common.waitingMarket")} body={t("common.waitingMarketBody")} />
  }
  return (
    <div className="market-strip" aria-label={t("overview.recentSnapshots")}>
      {snapshots.slice(0, 6).map((snapshot) => (
        <div className="market-strip-item" key={`${snapshot.symbol}-${snapshot.captured_at}`}>
          <span>{snapshot.symbol}</span>
          <strong>{formatNumber(snapshot.last_price, 2)}</strong>
          <small>{formatRelativeTime(snapshot.captured_at, locale)}</small>
        </div>
      ))}
    </div>
  )
}

export function DecisionCard({ decision }: { decision: Decision }) {
  const { t, locale } = useI18n()
  const proposal = decision.proposal
  const reasons = decision.risk_decision?.reasons ?? []
  return (
    <article className="decision-card">
      <div className="decision-card-header">
        <div>
          <span className="eyebrow">{t("common.latestCall")}</span>
          <h3><ActionMark action={decision.action} /> <span>{decision.symbol}</span></h3>
        </div>
        <RiskBadge status={decision.status} />
      </div>
      <p className="decision-reasoning">
        <ReasonText code={proposal?.reasoning_summary} fallback={t("common.noProposal")} />
      </p>
      <div className="decision-meta">
        <span>{t("common.confidence")} <b>{formatPercent(proposal?.confidence)}</b></span>
        <span>{t("common.size")} <b>{formatPercent(proposal?.position_size_pct)}</b></span>
        {/* 虚拟盘杠杆固定、系统不下发提案杠杆，展示账户实际值而非提案占位值。 */}
        <span title={t("console.leverageFixed")}>{t("common.leverage")} <b>{decision.leverage ?? proposal?.leverage ?? 1}×</b></span>
      </div>
      {reasons.length ? <div className="risk-reasons">{reasons.map((reason, index) => <span key={`${reason}-${index}`}>{index ? t("common.listSeparator") : ""}<ReasonText code={reason} /></span>)}</div> : null}
      <div className="decision-footer">
        <code>{decision.cycle_id.slice(0, 12)}</code>
        <span>{formatDate(decision.created_at, locale)}</span>
      </div>
    </article>
  )
}

export function PortfolioTable({ positions, onClose }: { positions: Position[]; onClose?: (symbol: string) => void }) {
  const { t } = useI18n()
  if (!positions.length) return <EmptyState title={t("common.noPositions")} body={t("common.noPositionsBody")} />
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>{t("common.contract")}</th><th>{t("common.side")}</th><th>{t("common.quantity")}</th><th>{t("common.entry")}</th><th>{t("common.mark")}</th><th title={t("trades.upnlHint")}>{t("common.upnl")}</th>{onClose ? <th>{t("common.action")}</th> : null}</tr></thead>
        <tbody>{positions.map((position) => <tr key={position.id}>
          <td><strong>{position.symbol}</strong><small>{position.status}</small></td>
          <td><ActionMark action={position.side} /></td>
          <td className="mono">{formatNumber(position.quantity, 5)}</td>
          <td className="mono">{formatNumber(position.entry_price, 2)}</td>
          <td className="mono">{position.mark_price == null ? "—" : formatNumber(position.mark_price, 2)}</td>
          <td className={`mono ${position.unrealized_pnl >= 0 ? "positive-text" : "negative-text"}`}>{formatSigned(position.unrealized_pnl, 2)}</td>
          {onClose ? <td>{position.status === "OPEN" ? <button className="button button--danger button--sm" onClick={() => onClose(position.symbol)}>{t("trades.closePosition")}</button> : null}</td> : null}
        </tr>)}</tbody>
      </table>
    </div>
  )
}

export function EventList({ events }: { events: RiskEvent[] }) {
  const { t, locale } = useI18n()
  if (!events.length) return <EmptyState title={t("common.noRiskEvents")} body={t("common.noRiskEventsBody")} />
  return <div className="event-list">{events.map((event) => <div className="event-row" key={event.id}>
    <div className="event-icon"><CircleAlert size={16} /></div>
    <div>
      <strong>{labelText(eventTypeLabel(event.event_type), t)}</strong>
      {/* 原因可能是后端用 `;` 拼起来的多条（entry_evidence_missing;hold_no_order），
          逐条翻译；认不出的部分（WEEX 报错原文）原样保留。 */}
      <p>{reasonParts(event.reason).map((part, index) => <span key={`${event.id}-${index}`}>{index ? t("common.listSeparator") : ""}{labelText(part, t)}</span>)}</p>
    </div>
    <RiskBadge status={event.status} />
    <time>{formatDate(event.created_at, locale)}</time>
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

export function formatDate(value: string | number | null | undefined, locale: Locale = "en-US") {
  if (!value) return translate(locale, "common.notRecorded")
  const date = new Date(typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value)
  if (Number.isNaN(date.getTime())) return translate(locale, "common.notRecorded")
  return date.toLocaleString(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
}

export function formatRelativeTime(value: string | number, locale: Locale = "en-US") {
  if (typeof value === "string") return formatDate(value, locale)
  const seconds = Math.max(0, Math.floor((Date.now() - (value < 10_000_000_000 ? value * 1000 : value)) / 1000))
  if (seconds < 60) return translate(locale, "common.secondsAgo", { n: seconds })
  return translate(locale, "common.minutesAgo", { n: Math.floor(seconds / 60) })
}

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
