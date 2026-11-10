import { expect, test } from "@playwright/test";

// No real Google or email sign-in here: CI has no test account (CHB-20).
// These checks hold before and after the owner applies the Identity Platform Terraform.

test("a visitor starts signed out", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByText("You are signed out.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});

test("closed API routes answer 401 without a token", async ({ request }) => {
  const response = await request.get("/api/me");

  expect(response.status()).toBe(401);
  expect(response.headers()["www-authenticate"]).toBe("Bearer");
});

test("public API routes answer without a token", async ({ request }) => {
  for (const path of ["/api/health", "/api/openapi.json"]) {
    expect((await request.get(path)).ok(), path).toBe(true);
  }
});
