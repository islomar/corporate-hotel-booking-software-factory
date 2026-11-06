import { expect, test } from "@playwright/test";

test("the page shows that the backend is up", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Corporate Hotel Booking" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Backend status: ok");
});

test("the API documentation is published", async ({ page, request }) => {
  const schema = await request.get("/api/openapi.json");
  expect(schema.ok()).toBe(true);

  await page.goto("/api/docs");
  await expect(page.getByText("Corporate Hotel Booking").first()).toBeVisible();
});
