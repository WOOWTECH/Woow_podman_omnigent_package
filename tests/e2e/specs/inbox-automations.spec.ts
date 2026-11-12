import type { Page } from "@playwright/test";
import { test, expect } from "../fixtures/auth";

// Route note: sidebar link labeled "Automations" points at `/tasks`.
async function openAutomations(page: Page): Promise<void> {
  // 0.21.0 greets a user's first visit with an "Introducing Automations" tour that covers the
  // page; "Maybe later" closes it for good. It can open after the page settles, so let
  // Playwright dismiss it whenever it shows up instead of checking once.
  await page.addLocatorHandler(
    page.getByRole("dialog", { name: /introducing automations/i }),
    async (tour) => {
      await tour.getByRole("button", { name: /^maybe later$/i }).click();
    },
  );
  await page.goto("/tasks");
}

test.describe("inbox", () => {
  test("inbox: shows 'Nothing waiting on you' empty state when zero pending", async ({
    page,
  }) => {
    await page.goto("/inbox");
    await expect(page.getByText(/nothing waiting on you/i)).toBeVisible();
  });
});

test.describe("automations", () => {
  test("automations: 'New automation' opens modal with Name / Session / Prompt / Frequency / Time / Host / Harness / Model fields", async ({
    page,
  }) => {
    await openAutomations(page);
    await page.getByRole("button", { name: /^new automation$/i }).click();
    const dialog = page.getByRole("dialog", { name: /^new automation$/i });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/^name$/i)).toBeVisible();
    // 0.21.0: each run starts a new session or continues one.
    await expect(dialog.getByRole("combobox", { name: /^session$/i })).toBeVisible();
    await expect(dialog.getByLabel(/^prompt$/i)).toBeVisible();
    await expect(dialog.getByRole("combobox", { name: /^frequency$/i })).toBeVisible();
    await expect(dialog.getByLabel(/^time$/i)).toBeVisible();
    // 0.21.0 replaced "Runs with" by an explicit host and harness.
    await expect(dialog.getByRole("combobox", { name: /^host$/i })).toBeVisible();
    await expect(dialog.getByText(/^harness$/i)).toBeVisible();
    await expect(dialog.getByRole("combobox", { name: /^model$/i })).toBeVisible();
  });

  test("automations: Cancel closes modal without creating an automation", async ({
    page,
  }) => {
    await openAutomations(page);
    const rowsBefore = await page.getByRole("row").count();
    await page.getByRole("button", { name: /^new automation$/i }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /^cancel$/i }).click();
    await expect(dialog).toBeHidden();
    const rowsAfter = await page.getByRole("row").count();
    expect(rowsAfter).toBe(rowsBefore);
  });
});
