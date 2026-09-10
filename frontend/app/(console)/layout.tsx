import { DashboardShell } from "@/components/dashboard-shell"

export default function ConsoleLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // 服务端组件读 process.env 是运行时求值，不会像客户端那样被构建时内联，
  // 因此换 Clerk 密钥不需要重新构建镜像。
  return (
    <DashboardShell clerkEnabled={Boolean(process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY)}>
      {children}
    </DashboardShell>
  )
}
