import { expect, test } from "@playwright/test"

test("landing page explains the virtual-only boundary", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByText("无实盘资金 · 无托管 · 模拟执行")).toBeVisible()
  await expect(page.getByRole("link", { name: "登录" })).toHaveAttribute("href", "/dashboard")
  await page.getByRole("button", { name: "切换语言" }).click()
  await expect(page.getByText("no live funds · no custody · simulated execution")).toBeVisible()
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/dashboard")
})
