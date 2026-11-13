import { expect, test, type Page } from "@playwright/test";

// Collects CSP violations, script errors and requests to other origins while a page loads
// (CHB-22). An empty list means the page ran under the CSP with only its own assets.
function watch(page: Page, baseURL: string | undefined) {
  const problems: string[] = [];
  const origin = new URL(baseURL ?? "http://localhost:8080").origin;
  page.on("console", (message) => {
    const text = message.text();
    if (/content security policy|violates the following/i.test(text)) {
      problems.push(`console: ${text}`);
    }
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("request", (request) => {
    const url = request.url();
    if (/^(data|blob):/.test(url)) return;
    if (new URL(url).origin !== origin) problems.push(`foreign request: ${url}`);
  });
  return problems;
}

test("the page shows that the backend is up", async ({ page, baseURL }) => {
  const problems = watch(page, baseURL);

  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Corporate Hotel Booking" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Backend status: ok");
  expect(problems).toEqual([]);
});

test("the API documentation renders under the CSP with its own assets", async ({
  page,
  request,
  baseURL,
}) => {
  const schema = await request.get("/api/openapi.json");
  expect(schema.ok()).toBe(true);
  const problems = watch(page, baseURL);

  await page.goto("/api/docs");

  await expect(page.locator(".swagger-ui .info .title")).toContainText(
    "Corporate Hotel Booking",
  );
  await expect(page.locator(".swagger-ui .opblock").first()).toBeVisible();
  expect(problems).toEqual([]);
});
