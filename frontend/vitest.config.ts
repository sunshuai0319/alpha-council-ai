import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": process.cwd(),
    },
  },
  test: {
    environment: "jsdom",
    exclude: ["node_modules/**", "tests/e2e/**"],
    setupFiles: ["./tests/setup.ts"],
  },
})
