import { describe, expect, it } from "vitest"

import { translate } from "@/lib/i18n"
import { labelText, reasonLabel, reasonParts } from "@/lib/labels"

const zh = (key: string, params?: Record<string, string>) => translate("zh-CN", key as never, params)
const en = (key: string, params?: Record<string, string>) => translate("en-US", key as never, params)

/** 走一遍「后端码 → 文案」，两个语言各来一次，确保两边都真有条目。 */
function render(code: string) {
  return { zh: labelText(reasonLabel(code), zh), en: labelText(reasonLabel(code), en) }
}

describe("reasonLabel", () => {
  it("translates the codes the backend added after the first pass", () => {
    // 这批曾经两边都没译，界面上直接冒英文码。后端 test_reasons.py 的
    // test_card_table_never_exceeds_the_web_table 保证卡片译表不超出这里。
    expect(render("entry_stop_loss_missing").zh).toBe("提案缺少止损")
    expect(render("entry_signal_evidence_missing").zh).toBe("提案缺少入场证据引用")
    expect(render("exchange_position_missing").zh).toBe("本地有持仓、交易所无，暂不开新仓")
    expect(render("reentry_cooldown").zh).toBe("刚平仓，冷却期内不开新仓")
    expect(render("manual_reduce_only").zh).toBe("人工减仓")
  })

  it("translates the data-integrity evidence codes", () => {
    expect(render("crossed_book").zh).toBe("盘口倒挂（买价高于卖价）")
    expect(render("inverted_24h_range").zh).toBe("24 小时最高价低于最低价")
    expect(render("last_price_outside_24h_range").zh).toBe("最新价落在 24 小时区间之外")
  })

  it("keeps the parameters of a spread problem", () => {
    // 后端是 f-string（`spread_bps={value:.1f}>{MAX_SPREAD_BPS}`），不是枚举码。
    expect(render("spread_bps=62.5>50.0").zh).toBe("盘口点差 62.5 bps，超过 50.0 bps 上限")
    expect(render("spread_bps=62.5>50.0").en).toBe("Book spread 62.5 bps exceeds the 50.0 bps cap")
  })

  it("keeps the legacy evidence-missing key for rows written before the rename", () => {
    expect(render("entry_evidence_missing").zh).toBe("缺少证据引用")
  })

  it("translates the fail-closed veto prefix", () => {
    // graph.py 的 fail-closed 走 `_hold_proposal(current, f"veto_fail_closed:{reasons}")`。
    expect(render("veto_fail_closed:news_macro").zh).toBe("否决链异常，已安全观望：news_macro")
    expect(render("veto_fail_closed:news_macro").en).toBe("Veto chain failed closed; holding: news_macro")
  })

  it("localizes the rule-signal direction", () => {
    // 中文界面上留一个英文 `SHORT` 属于漏译；卡片那边一直用「做空」。
    expect(render("rule signal SHORT score=-0.5477").zh).toBe("规则信号 做空，分数 -0.55")
    expect(render("rule signal LONG score=0.31").zh).toBe("规则信号 做多，分数 0.31")
    expect(render("rule signal SHORT score=-0.5477").en).toBe("Rule signal short, score -0.55")
  })

  it("takes a semicolon-joined summary apart and translates each code", () => {
    // 后端的两种拼接写法都要能拆：`_persist` 用 ";", graph.py 用 "; "。
    const parts = (code: string) =>
      reasonParts(code).map((part) => labelText(part, zh)).join("、")

    expect(parts("entry_stop_loss_missing;entry_signal_evidence_missing")).toBe(
      "提案缺少止损、提案缺少入场证据引用",
    )
    expect(parts("entry_stop_loss_missing; entry_signal_evidence_missing")).toBe(
      "提案缺少止损、提案缺少入场证据引用",
    )
    // 拆完只剩空白段时要当作没内容，不能多渲染一个分隔符。
    expect(reasonParts("hold_no_order; ")).toHaveLength(1)
  })

  it("shows an unknown code as-is instead of blank", () => {
    expect(render("some_future_code")).toEqual({ zh: "some_future_code", en: "some_future_code" })
  })

  it("every translated code is localized in both locales", () => {
    // 只补了 zh-CN 而漏了 en-US 的话，英文界面会显示 message key 本身；
    // 只补了 en-US 而漏了 zh-CN 的话，中文界面会退回英文（translate 会 fallback）。
    const codes = [
      "entry_stop_loss_missing",
      "entry_signal_evidence_missing",
      "exchange_position_missing",
      "reentry_cooldown",
      "manual_reduce_only",
      "crossed_book",
      "inverted_24h_range",
      "last_price_outside_24h_range",
      "spread_bps=62.5>50.0",
    ]
    for (const code of codes) {
      const { zh: zhText, en: enText } = render(code)
      expect(zhText).not.toMatch(/^reason\./)
      expect(enText).not.toMatch(/^reason\./)
      expect(zhText).not.toBe(code)
      expect(enText).not.toBe(code)
      expect(zhText).not.toBe(enText)
    }
  })
})
