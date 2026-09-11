export type DashboardView = "overview" | "market" | "committee" | "trades" | "events"

export type MarketSnapshot = {
  symbol: string
  captured_at: string | number
  last_price: number
  bid?: number | null
  ask?: number | null
  funding_rate?: number | null
  open_interest?: number | null
  volume_24h?: number | null
}

export type Analysis = {
  status?: string
  confidence?: number
  reasoning_summary?: string
  evidence_refs?: string[]
  model_version?: string
  trace_id?: string
}

export type TradeProposal = {
  action?: string
  symbol?: string
  side?: string | null
  position_size_pct?: number
  leverage?: number
  stop_loss?: number | null
  take_profit?: number | null
  valid_until?: number | null
  invalidation_conditions?: string[]
  confidence?: number
  reasoning_summary?: string
  evidence_refs?: string[]
  model_version?: string
  trace_id?: string
}

export type Decision = {
  id: string
  cycle_id: string
  symbol: string
  action: string
  status: string
  proposal?: TradeProposal | null
  analyses?: {
    market?: Analysis | null
    quant?: Analysis | null
    macro?: Analysis | null
  } | null
  risk_decision?: {
    status?: string
    reasons?: string[]
    adjusted_position_size_pct?: number | null
  } | null
  execution_result?: {
    status?: string
    client_order_id?: string
    exchange_order_id?: string | null
    message?: string | null
  } | null
  //: 账户实际生效杠杆（虚拟盘固定，系统不下发提案杠杆）；显示用，不是提案值。
  leverage?: number | null
  created_at?: string | null
}

export type Position = {
  id: string
  symbol: string
  side: string
  quantity: number
  entry_price: number
  mark_price?: number | null
  unrealized_pnl: number
  status: string
}

export type RiskEvent = {
  id: string
  event_type: string
  status: string
  reason: string
  created_at?: string | null
}

export type RiskLimits = {
  max_position_notional_pct?: number
  max_single_trade_risk_pct?: number
  max_daily_loss_pct?: number
  max_consecutive_losses?: number
}

export type PlatformLimits = {
  max_position_notional_pct: number
  max_single_trade_risk_pct: number
  max_daily_loss_pct: number
  max_consecutive_losses: number
}

export type EffectiveRiskLimits = PlatformLimits & { max_leverage: number }

export type AccountCredentials = {
  api_key: string
  api_secret: string
  passphrase: string
}

export type TradingAccount = {
  id: string
  provider: string
  environment: string
  enabled: boolean
  configured: boolean
  credentials: AccountCredentials | null
  risk_limits: RiskLimits | null
  effective_risk_limits: EffectiveRiskLimits | null
  platform_limits: PlatformLimits | null
}

export type ItemsResponse<T> = {
  items: T[]
}
