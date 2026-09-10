import { ClerkProvider } from "@clerk/nextjs"
import type { Metadata } from "next"

import { I18nProvider } from "@/lib/i18n"

import "./globals.css"

const clerkPublishableKey = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY

export const metadata: Metadata = {
  title: "Alpha Council AI",
  description: "AI investment committee for WEEX virtual futures",
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <I18nProvider>
          {clerkPublishableKey ? <ClerkProvider publishableKey={clerkPublishableKey}>{children}</ClerkProvider> : children}
        </I18nProvider>
      </body>
    </html>
  )
}
