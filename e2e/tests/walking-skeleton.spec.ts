import { expect, test } from "@playwright/test";

test("the page shows that the backend is up", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Corporate Hotel Booking" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Backend status: ok");
});
