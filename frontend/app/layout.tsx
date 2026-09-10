import { ClerkProvider } from "@clerk/nextjs"
import type { Metadata } from "next"

import "./globals.css"

const clerkPublishableKey = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY

export const metadata: Metadata = {
  title: "Alpha Council AI",
  description: "AI investment committee for WEEX virtual futures",
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        {clerkPublishableKey ? <ClerkProvider publishableKey={clerkPublishableKey}>{children}</ClerkProvider> : children}
      </body>
    </html>
  )
}
