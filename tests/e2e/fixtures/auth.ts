import { test as base, expect, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

/**
 * Auth fixture — logs in once per worker, then hands every test an already
 * authenticated `page`. We stash `storageState` on disk (per-worker) so
 * repeated tests reuse the session cookie instead of hammering /login.
 *
 * The password has NO default: it is the deployment's admin credential and must come from
 * the environment (see tests/e2e/README.md). Read it out of the podman secret, e.g.
 *   OMNIGENT_ADMIN_PASSWORD=$(podman secret inspect --showsecret \
 *       --format '{{.SecretData}}' omnigent-admin-password) npm test
 */

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. See tests/e2e/README.md: this suite runs against a real ` +
        `deployment and has no built-in credentials.`,
    );
  }
  return value;
}

const USERNAME = process.env.OMNIGENT_ADMIN_USERNAME ?? "admin";
const PASSWORD = requiredEnv("OMNIGENT_ADMIN_PASSWORD");

async function performLogin(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(USERNAME);
  await page.getByLabel(/password/i).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in|log in|login/i }).click();
  // Home renders the "What should we build?" hero once auth completes.
  await expect(
    page.getByText(/what should we build/i),
  ).toBeVisible({ timeout: 20_000 });
}

type AuthFixtures = {
  storageStatePath: string;
  page: Page;
};

export const test = base.extend<{}, AuthFixtures>({
  storageStatePath: [
    async ({ browser }, use, workerInfo) => {
      const statePath = path.join(
        os.tmpdir(),
        `omnigent-e2e-storage-w${workerInfo.workerIndex}.json`,
      );
      if (!fs.existsSync(statePath)) {
        const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
        const page = await ctx.newPage();
        await performLogin(page);
        await ctx.storageState({ path: statePath });
        await ctx.close();
      }
      await use(statePath);
    },
    { scope: "worker" },
  ],

  page: async ({ browser, storageStatePath }, use) => {
    const ctx = await browser.newContext({
      storageState: storageStatePath,
      ignoreHTTPSErrors: true,
    });
    const page = await ctx.newPage();
    await use(page);
    await ctx.close();
  },
});

export { expect };
