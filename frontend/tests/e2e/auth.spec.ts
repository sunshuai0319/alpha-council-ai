import { expect, test } from "@playwright/test"

test("landing page explains the virtual-only boundary", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByText("no live funds · no custody · simulated execution")).toBeVisible()
  await expect(page.getByRole("link", { name: "Open console" })).toHaveAttribute("href", "/dashboard")
})
