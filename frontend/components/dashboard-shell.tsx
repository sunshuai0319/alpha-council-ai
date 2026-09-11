"use client"

import { Activity, BrainCircuit, ChartNoAxesCombined, CircleAlert, LayoutDashboard, Radio, Settings, ShieldCheck } from "lucide-react"
import { UserButton } from "@clerk/nextjs"
import Link from "next/link"
import { usePathname } from "next/navigation"

import { VirtualBadge } from "@/components/console-primitives"
import { LanguageSwitcher } from "@/components/language-switcher"
import { useI18n } from "@/lib/i18n"

// 由服务端 layout 传入，不在这里读 process.env：客户端组件里的 NEXT_PUBLIC_*
// 会被 Next 在**构建时**内联，导致换密钥必须重新构建镜像。
export function DashboardShell({
  children,
  clerkEnabled,
}: {
  children: React.ReactNode
  clerkEnabled: boolean
}) {
  const pathname = usePathname()
  const { t } = useI18n()
  const links = [
    { href: "/dashboard", label: t("nav.overview"), icon: LayoutDashboard },
    { href: "/market", label: t("nav.market"), icon: Activity },
    { href: "/committee", label: t("nav.committee"), icon: BrainCircuit },
    { href: "/trades", label: t("nav.trades"), icon: ChartNoAxesCombined },
    { href: "/events", label: t("nav.events"), icon: ShieldCheck },
  ]
  return (
    <div className="console-frame">
      <aside className="sidebar">
        <Link className="brand" href="/dashboard">
          <span className="brand-mark"><Radio size={17} /></span>
          <span><b>alpha council</b><small>{t("brand.tagline")}</small></span>
        </Link>
        <div className="sidebar-rule" />
        <div className="sidebar-caption">{t("nav.workspace")}</div>
        <span className="sidebar-mobile-badge">{t("nav.simulation")}</span>
        <nav className="primary-nav" aria-label={t("nav.primary")}>
          {links.map(({ href, label, icon: Icon }) => {
            const active = pathname === href
            return <Link className={active ? "nav-link nav-link--active" : "nav-link"} href={href} key={href}>
              <Icon size={17} strokeWidth={active ? 2.4 : 1.8} />
              <span>{label}</span>
              {active ? <i /> : null}
            </Link>
          })}
        </nav>
        <div className="sidebar-bottom">
          <div className="guardrail-note"><CircleAlert size={16} /><span>{t("landing.boundary")}</span></div>
          <VirtualBadge />
          <div className="account-row"><Link className="account-settings" href="/settings" aria-label={t("nav.accountSettings")} title={t("nav.accountSettings")}><Settings size={14} /></Link><LanguageSwitcher />{clerkEnabled ? <UserButton afterSignOutUrl="/" /> : <span className="account-placeholder">●</span>}<span>{t("nav.account")}</span></div>
        </div>
      </aside>
      <main className="main-canvas">{children}</main>
    </div>
  )
}
