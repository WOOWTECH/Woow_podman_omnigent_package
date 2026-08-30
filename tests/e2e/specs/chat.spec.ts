import { test, expect } from "../fixtures/auth";

/**
 * Chat spec — serial: creating parallel sessions collides with the sidebar
 * "recent sessions" refresh and produces flaky ordering assertions.
 */
test.describe.serial("chat", () => {
  test("chat: create new session, send prompt, receive pi reply within 90s", async ({
    page,
  }) => {
    const nanoid = Math.random().toString(36).slice(2, 10);
    const marker = `playwright-smoke-${nanoid}`;
    const prompt = `Reply with exactly the word: ${marker}`;

    await page.goto("/");
    await page
      .getByRole("link", { name: /^new session$/i })
      .or(page.getByRole("button", { name: /^new session$/i }))
      .first()
      .click();

    const composer = page
      .getByRole("textbox", { name: /message|prompt|what should we build/i })
      .first();
    await expect(composer).toBeVisible();
    await composer.fill(prompt);
    await composer.press("Enter");

    // Pi replies via the runner harness — allow up to 90s for the model round-trip.
    await expect(page.getByText(marker, { exact: false })).toBeVisible({
      timeout: 90_000,
    });
  });

  test("chat: harness picker menu shows Pi + Polly + Debby + Create custom agent", async ({
    page,
  }) => {
    await page.goto("/");
    // Harness picker is the composer's leading "Runs with" / harness button.
    const picker = page
      .getByRole("button", { name: /pi|harness|runs with/i })
      .first();
    await picker.click();
    const menu = page.getByRole("menu").or(page.getByRole("listbox"));
    await expect(menu).toBeVisible();
    await expect(menu.getByText(/^pi$/i)).toBeVisible();
    await expect(menu.getByText(/^polly$/i)).toBeVisible();
    await expect(menu.getByText(/^debby$/i)).toBeVisible();
    await expect(menu.getByText(/create custom agent/i)).toBeVisible();
  });

  test("chat: keyboard shortcut Ctrl+N navigates to New session", async ({
    page,
  }) => {
    await page.goto("/settings/account");
    await expect(page.getByRole("heading", { name: /account/i })).toBeVisible();
    await page.keyboard.press("Control+n");
    await expect(page.getByText(/what should we build/i)).toBeVisible({
      timeout: 10_000,
    });
  });
});
