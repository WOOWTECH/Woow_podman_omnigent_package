import { test, expect } from "../fixtures/auth";

test.describe("inbox", () => {
  test("inbox: shows 'Nothing waiting on you' empty state when zero pending", async ({
    page,
  }) => {
    await page.goto("/inbox");
    await expect(page.getByText(/nothing waiting on you/i)).toBeVisible();
  });
});

test.describe("automations", () => {
  test("automations: 'New task' opens modal with Name / Prompt / Runs with / Model / Frequency / Time fields", async ({
    page,
  }) => {
    await page.goto("/automations");
    await page.getByRole("button", { name: /new task/i }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/^name$/i)).toBeVisible();
    await expect(dialog.getByLabel(/^prompt$/i)).toBeVisible();
    await expect(dialog.getByLabel(/runs with/i)).toBeVisible();
    await expect(dialog.getByLabel(/^model$/i)).toBeVisible();
    await expect(dialog.getByLabel(/frequency/i)).toBeVisible();
    await expect(dialog.getByLabel(/^time$/i)).toBeVisible();
  });

  test("automations: Cancel closes modal without creating a task", async ({
    page,
  }) => {
    await page.goto("/automations");
    const rowsBefore = await page.getByRole("row").count();
    await page.getByRole("button", { name: /new task/i }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /^cancel$/i }).click();
    await expect(dialog).toBeHidden();
    const rowsAfter = await page.getByRole("row").count();
    expect(rowsAfter).toBe(rowsBefore);
  });
});
