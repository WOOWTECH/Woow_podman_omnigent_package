import { test, expect } from "@playwright/test";

/** The runner host belongs to operator, so admin cannot start a Pi session on it. */
const USERNAME = process.env.OMNIGENT_CHAT_USERNAME ?? "operator";

test.describe.serial("chat as runner owner", () => {
  test.beforeEach(async ({ page }) => {
    const password = process.env.OMNIGENT_CHAT_PASSWORD;
    if (!password) {
      throw new Error(
        "OMNIGENT_CHAT_PASSWORD is required for chat E2E. See tests/e2e/README.md.",
      );
    }
    await page.goto("/login");
    await page.getByLabel(/username/i).fill(USERNAME);
    await page.getByLabel(/password/i).fill(password);
    await page.getByRole("button", { name: /sign in|log in|login/i }).click();
    await expect(page.getByText(/what should we build/i)).toBeVisible({
      timeout: 20_000,
    });
  });

  test("chat: Pi returns an assistant message through the connected runner", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    const marker = `playwright-smoke-${Math.random().toString(36).slice(2, 10)}`;
    const prompt = `Reply with exactly the word: ${marker}`;

    await expect(page.getByTestId("new-chat-landing-host-chip")).toHaveAttribute(
      "aria-label",
      /online/i,
    );
    await page.getByTestId("new-chat-landing-agent-select").click();
    await page.getByRole("menuitem", { name: /other/i }).first().click();
    const pi = page.getByRole("menuitem", { name: "Pi", exact: true });
    await expect(pi, "Pi must be ready on the connected runner").toBeEnabled();
    await pi.click();
    await expect(page.getByTestId("new-chat-landing-agent-select")).toHaveAttribute(
      "aria-label",
      /pi/i,
    );

    await page.getByTestId("new-chat-landing-input").fill(prompt);
    await page.getByTestId("new-chat-landing-submit").click();
    // Omnigent first navigates to /c/temp:<id>, then replaces that URL when
    // the server commits the real session. Only the latter has an API record.
    await expect(page).toHaveURL(/\/c\/(?!temp:)[a-f0-9]{32}(?:[/?#]|$)/, {
      timeout: 30_000,
    });
    const sessionId = new URL(page.url()).pathname.match(/\/c\/([a-f0-9]{32})/)?.[1];
    expect(sessionId, `Expected a session ID in ${page.url()}`).toBeTruthy();
    const sessionUrl = new URL(`/v1/sessions/${sessionId}`, page.url()).toString();

    // The user's own prompt contains marker. A committed assistant item must
    // exist before the rendered answer can satisfy this test.
    await expect.poll(async () => {
      const response = await page.request.get(sessionUrl);
      if (!response.ok()) {
        throw new Error(`Session read failed: HTTP ${response.status()} at ${sessionUrl}`);
      }
      const session = await response.json();
      const replies = (session.items ?? []).filter(
        (item: { type: string; data?: { role?: string } }) =>
          item.type === "message" && item.data?.role === "assistant",
      );
      return replies.flatMap(
        (item: { data: { content: Array<{ text?: string }> } }) =>
          item.data.content.map((part) => part.text ?? ""),
      ).join("").trim();
    }, { timeout: 90_000, intervals: [1_000, 2_000, 5_000] }).toBe(marker);
    await expect(page.getByText(marker, { exact: true })).toBeVisible();
  });

  test("chat: harness picker lists Pi, Polly, and Debby", async ({ page }) => {
    await page.getByTestId("new-chat-landing-agent-select").click();
    const menu = page.getByRole("menu");
    await expect(menu.getByText(/^polly$/i)).toBeVisible();
    await expect(menu.getByText(/^debby$/i)).toBeVisible();
    await menu.getByRole("menuitem", { name: /other/i }).first().click();
    await expect(menu.getByRole("menuitem", { name: "Pi", exact: true })).toBeVisible();
  });

  test("chat: Ctrl+Shift+O opens a new session", async ({ page }) => {
    await page.goto("/settings/account");
    await expect(page.getByRole("heading", { name: /account/i })).toBeVisible();
    await page.keyboard.press("Control+Shift+O");
    await expect(page.getByText(/what should we build/i)).toBeVisible({
      timeout: 10_000,
    });
  });
});
