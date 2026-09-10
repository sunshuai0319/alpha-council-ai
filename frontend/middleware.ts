import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server"

const isProtectedRoute = createRouteMatcher(["/dashboard(.*)", "/market(.*)", "/committee(.*)", "/trades(.*)", "/events(.*)"])

export default clerkMiddleware(async (auth, request) => {
  if (isProtectedRoute(request)) await auth.protect()
})

export const config = {
  matcher: ["/((?!_next|.*\\..*).*)"],
}
