"use client"

import { ArrowUpRight, Radio, ShieldCheck } from "lucide-react"
import Link from "next/link"

import { LanguageSwitcher } from "@/components/language-switcher"
import { useI18n } from "@/lib/i18n"

export default function HomePage() {
  const { t } = useI18n()

  return <main className="landing-page">
    <nav className="landing-nav"><Link className="brand brand--landing" href="/"><span className="brand-mark"><Radio size={17} /></span><span><b>alpha council</b><small>{t("brand.tagline")}</small></span></Link><div><LanguageSwitcher /><Link className="landing-button" href="/dashboard">{t("landing.signIn")} <ArrowUpRight size={15} /></Link></div></nav>
    <section className="landing-hero"><div className="landing-kicker"><span className="status-dot" /> {t("landing.kicker")}</div><h1 dangerouslySetInnerHTML={{ __html: t("landing.title") }} /><p>{t("landing.description")}</p><div className="landing-actions"><Link className="button button--signal" href="/dashboard">{t("landing.enterLab")} <ArrowUpRight size={16} /></Link><span><ShieldCheck size={15} /> {t("landing.boundary")}</span></div></section>
    <section className="landing-grid"><div><span className="eyebrow">01 / {t("landing.observe")}</span><strong>{t("landing.observeTitle")}</strong><p>{t("landing.observeBody")}</p></div><div><span className="eyebrow">02 / {t("landing.debate")}</span><strong>{t("landing.debateTitle")}</strong><p>{t("landing.debateBody")}</p></div><div><span className="eyebrow">03 / {t("landing.constrain")}</span><strong>{t("landing.constrainTitle")}</strong><p>{t("landing.constrainBody")}</p></div></section>
  </main>
}
