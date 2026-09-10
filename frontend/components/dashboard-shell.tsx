"use client"

import { Activity, BrainCircuit, ChartNoAxesCombined, CircleAlert, LayoutDashboard, Radio, ShieldCheck } from "lucide-react"
import { UserButton } from "@clerk/nextjs"
import Link from "next/link"
import { usePathname } from "next/navigation"

import { VirtualBadge } from "@/components/console-primitives"

const hasClerkKey = Boolean(process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY)

const links = [
  { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/market", label: "Market pulse", icon: Activity },
  { href: "/committee", label: "AI committee", icon: BrainCircuit },
  { href: "/trades", label: "Trade ledger", icon: ChartNoAxesCombined },
  { href: "/events", label: "Risk events", icon: ShieldCheck },
]

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  return (
    <div className="console-frame">
      <aside className="sidebar">
        <Link className="brand" href="/dashboard">
          <span className="brand-mark"><Radio size={17} /></span>
          <span><b>alpha council</b><small>AI trading lab</small></span>
        </Link>
        <div className="sidebar-rule" />
        <div className="sidebar-caption">workspace</div>
        <nav className="primary-nav" aria-label="Primary navigation">
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
          <div className="guardrail-note"><CircleAlert size={16} /><span>All orders are simulated.<br />No real funds are at risk.</span></div>
          <VirtualBadge />
          <div className="account-row">{hasClerkKey ? <UserButton afterSignOutUrl="/" /> : <span className="account-placeholder">●</span>}<span>Account</span></div>
        </div>
      </aside>
      <main className="main-canvas">{children}</main>
    </div>
  )
}
