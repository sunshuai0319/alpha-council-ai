import type { MarketSnapshot, TradingAccount } from "@/lib/types"

/**
 * 概览拿不到行情数据时，说明原因 —— 只显示「—」会让用户以为界面坏了。
 *
 * 这些表只在交易周期里写入，而周期只对已启用的账户运行，所以「没数据」几乎
 * 总是这三种情况之一，且对应着不同的补救动作。
 */
export function emptyOverviewHint(
  accounts: TradingAccount[],
  market: MarketSnapshot[],
): string | null {
  if (market.length > 0) return null
  if (accounts.length === 0) return "overview.hintNoAccount"
  if (!accounts.some((account) => account.enabled)) return "overview.hintAccountDisabled"
  return "overview.hintAwaitingCycle"
}
