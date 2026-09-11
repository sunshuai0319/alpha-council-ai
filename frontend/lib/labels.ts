/**
 * 后端的内部码 → 界面文案。
 *
 * 后端存的是**稳定的英文机器码**（`hold_no_order`、`safe-hold`、`signal_hold_score_0.26`…）——
 * 那是审计和查询要用的标识，不该为了显示而本地化。界面在这里翻译，
 * 于是选中文就显示中文、选英文显示英文，而数据库里永远是同一套码。
 *
 * 认不出来的码**原样显示**，不要吞掉 —— 展示一个陌生码远好过显示空白，
 * 而且它本身就是排查线索。
 */

/** 精确匹配的码。 */
const REASON_KEYS: Record<string, string> = {
  // 观望与账户状态
  hold_no_order: "reason.holdNoOrder",
  paused: "reason.paused",
  trading_disabled: "reason.tradingDisabled",

  // 行情与账户可用性
  market_data_stale: "reason.marketDataStale",
  market_snapshot_missing: "reason.marketSnapshotMissing",
  market_snapshot_stale: "reason.marketSnapshotStale",
  account_or_market_missing: "reason.accountOrMarketMissing",
  proposal_missing: "reason.proposalMissing",
  entry_non_positive: "reason.entryNonPositive",

  // 风控拒绝
  position_already_open: "reason.positionAlreadyOpen",
  max_notional: "reason.maxNotional",
  max_position_notional: "reason.maxPositionNotional",
  max_leverage: "reason.maxLeverage",
  single_trade_risk: "reason.singleTradeRisk",
  stop_loss_required: "reason.stopLossRequired",
  daily_trade_limit: "reason.dailyTradeLimit",
  daily_loss_limit: "reason.dailyLossLimit",
  consecutive_loss_cooldown: "reason.consecutiveLossCooldown",
  equity_non_positive: "reason.equityNonPositive",
  long_stop_must_be_below_entry: "reason.longStopBelow",
  short_stop_must_be_above_entry: "reason.shortStopAbove",
  long_take_profit_must_be_above_entry: "reason.longTakeProfitAbove",
  short_take_profit_must_be_below_entry: "reason.shortTakeProfitBelow",
  reward_risk_too_low: "reason.rewardRiskTooLow",
  entry_position_size_missing: "reason.entrySizeMissing",
  entry_evidence_missing: "reason.entryEvidenceMissing",
  proposal_expired: "reason.proposalExpired",
  proposal_symbol_mismatch: "reason.proposalSymbolMismatch",

  // 委员会 / 信号器
  committee_invalid_json_or_schema: "reason.committeeInvalid",
  committee_llm_not_configured: "reason.committeeLlmMissing",
  signal_missing_atr_or_price: "reason.signalMissingAtr",
  safe_hold: "reason.safeHold",

  // 否决（VetoReason 枚举）
  NEWS_SHOCK: "veto.NEWS_SHOCK",
  REGIME_CONFLICT: "veto.REGIME_CONFLICT",
  STRUCTURE_INVALIDATED: "veto.STRUCTURE_INVALIDATED",
  LIQUIDITY_ANOMALY: "veto.LIQUIDITY_ANOMALY",
  DATA_INTEGRITY: "veto.DATA_INTEGRITY",

  // 否决结局
  veto_none: "vetoOutcome.none",
  veto_applied: "vetoOutcome.applied",
  veto_invalid_ignored: "vetoOutcome.invalidIgnored",
};

/** 带参数的码：前缀 + 参数名。 */
const PREFIX_KEYS: Array<[string, string]> = [
  ["signal_hold_score_", "reason.signalHoldScore"],
  ["account_unavailable:", "reason.accountUnavailable"],
  ["retrieval_failed:", "reason.retrievalFailed"],
  ["vetoed:", "reason.vetoed"],
];

/** 模型版本 / 信号来源。 */
const MODEL_KEYS: Record<string, string> = {
  "rule-signal-v1": "model.ruleSignal",
  "safe-hold": "model.safeHold",
  "deterministic-fallback": "model.fallback",
};

export type Label = { key: string; params?: Record<string, string> } | { text: string };

function labelFor(code: string, table: Record<string, string>, prefixTable = PREFIX_KEYS): Label {
  if (table[code]) return { key: table[code] };
  for (const [prefix, key] of prefixTable) {
    if (code.startsWith(prefix)) {
      const rest = code.slice(prefix.length);
      if (prefix === "signal_hold_score_") {
        const score = Number(rest);
        // 分数要能显示成 0.26，不是 0.26000000000001
        return { key, params: { score: Number.isFinite(score) ? String(Number(score.toFixed(2))) : rest } };
      }
      return { key, params: { detail: rest } };
    }
  }
  return { text: code };
}

/** 风控拒绝 / 观望原因。 */
export function reasonLabel(code: string): Label {
  return labelFor(code, REASON_KEYS);
}

/** 模型版本。旧版委员会有 v1 / v1.0 / v1.0.0 三种写法。 */
export function modelLabel(version: string): Label {
  if (version.startsWith("committee-agent")) return { key: "model.committee" };
  return labelFor(version, MODEL_KEYS, []);
}

/** 把 Label 解析成可直接渲染的字符串。 */
export function render(
  label: Label,
  t: (key: string, params?: Record<string, string>) => string,
): string {
  return "key" in label ? t(label.key, label.params) : label.text;
}
