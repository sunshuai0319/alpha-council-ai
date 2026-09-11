export type DashboardView = "overview" | "market" | "committee" | "trades" | "events" | "settings"

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

/** 一路否决 agent 的独立结论（新闻宏观 / 结构流动性 / 数据完整性）。 */
export type VetoVerdict = {
  veto?: boolean
  status?: string
  reasons?: string[]
  evidence_refs?: string[]
  reasoning_summary?: string
}

export type VetoVerdicts = Record<string, VetoVerdict | null>

export type Decision = {
  id: string
  cycle_id: string
  symbol: string
  action: string
  status: string
  proposal?: TradeProposal | null
  analyses?: {
    //: market/quant/macro 是旧委员会架构的字段，当前 graph 不再产出（恒为 null），
    //: 保留声明以免旧记录报错；页面只渲染 veto_verdicts。
    market?: Analysis | null
    quant?: Analysis | null
    macro?: Analysis | null
    veto_verdicts?: VetoVerdicts | null
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
    //: 成交均价与平仓回合盈亏。后端用 model_dump(mode="json") 序列化，
    //: Decimal 会变成字符串，所以两者都可能是 string。
    average_price?: string | number | null
    realized_pnl?: string | number | null
  } | null
  //: 这次决策实际使用的模型（committee 等）。智囊团 banner 的模型路由读它，
  //: 不再把模型名写死在文案里 —— 换模型时历史决策要显示当时的模型。
  model_versions?: Record<string, string> | null
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

export type PaginatedResponse<T> = {
  items: T[]
  total: number
  page: number
  page_size: number
  //: 该用户历史上出现过的品种，供前端做数据驱动的筛选项（决策列表专用）。
  symbols?: string[]
}
