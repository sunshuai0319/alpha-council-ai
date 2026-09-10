import { describe, expect, it } from "vitest"

import { getInitialLocale, translate } from "@/lib/i18n"

describe("i18n", () => {
  it("defaults to Simplified Chinese when no saved locale exists", () => {
    expect(getInitialLocale(null)).toBe("zh-CN")
  })

  it("translates the sign-in entry in both supported locales", () => {
    expect(translate("zh-CN", "auth.signIn")).toBe("登录")
    expect(translate("en-US", "auth.signIn")).toBe("Sign in")
  })

  it("falls back to the key for missing translations", () => {
    expect(translate("zh-CN", "missing.key")).toBe("missing.key")
  })
})
