import { DashboardShell } from "@/components/dashboard-shell"

export default function ConsoleLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <DashboardShell>{children}</DashboardShell>
}
