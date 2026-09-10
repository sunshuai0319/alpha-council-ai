"use client"

import { Languages } from "lucide-react"

import { useI18n } from "@/lib/i18n"

export function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n()
  const nextLocale = locale === "zh-CN" ? "en-US" : "zh-CN"

  return (
    <button
      className="language-switcher"
      type="button"
      onClick={() => setLocale(nextLocale)}
      aria-label={t("language.switchTo")}
      title={t("language.switchTo")}
    >
      <Languages size={14} />
      <span>{locale === "zh-CN" ? t("language.zh") : t("language.en")}</span>
    </button>
  )
}
