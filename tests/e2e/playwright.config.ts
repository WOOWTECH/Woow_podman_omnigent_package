import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for Omnigent 0.11.0 Web UI E2E suite.
 *
 * - Chromium-only so the same suite runs on arm64/amd64 CI (Woow_podman runners).
 * - `baseURL` comes from OMNIGENT_BASE_URL and has no default: point it at the deployment
 *   under test (http://127.0.0.1:8000 over an SSH forward, or its tailnet URL).
 * - `ignoreHTTPSErrors: true` — Tailscale funnel serves a real cert but leaving this
 *   on prevents surprise flakes when CI resolves the tailnet host with a stale root.
 * - Serialized (workers: 1) so chat-session tests don't race the sidebar session list.
 */
const BASE_URL = process.env.OMNIGENT_BASE_URL;
if (!BASE_URL) {
  throw new Error(
    "OMNIGENT_BASE_URL is not set, e.g. OMNIGENT_BASE_URL=http://127.0.0.1:8000 npm test",
  );
}

export default defineConfig({
  testDir: "./specs",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: BASE_URL,
    ignoreHTTPSErrors: true,
    trace: "on-first-retry",
    video: "off",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
