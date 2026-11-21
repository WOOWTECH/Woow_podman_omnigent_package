import { test, expect } from "../fixtures/auth";

// Generated links contain bearer credentials. Never put this page or its requests
// in screenshots, traces or videos. Assertions below only report booleans/statuses.
test.use({ trace: "off", screenshot: "off", video: "off" });

test("admin can create a canonical MCP link and revoke it", async ({ page, playwright }) => {
  const expectedBase = process.env.OMNIGENT_MCP_PUBLIC_BASE_URL;
  test.skip(!expectedBase, "Set the candidate's complete external MCP base to exercise the URL contract");
  let profileId: string | undefined;
  let profileApi: string | undefined;
  const anonymous = await playwright.request.newContext();
  try {
    await page.goto("/settings/mcp-server");
    await expect(page.getByRole("heading", { name: "Omnigent MCP", exact: true })).toBeVisible();
    await page.getByLabel("Profile name", { exact: true }).fill(`podman-mcp-e2e-${Date.now()}`);
    await page.getByLabel("Omnigent account", { exact: true }).fill(process.env.OMNIGENT_ADMIN_USERNAME ?? "admin");
    await page.getByLabel("Account password", { exact: true }).fill(process.env.OMNIGENT_ADMIN_PASSWORD!);
    const pending = page.waitForResponse(r => r.url().endsWith("/v1/admin/mcp-server/profiles") && r.request().method() === "POST");
    await page.getByRole("button", { name: "Create MCP link", exact: true }).click();
    const created = await pending;
    expect(created.status()).toBe(201);
    const body = await created.json();
    profileId = body.profile?.id;
    profileApi = created.url();
    expect(typeof profileId === "string").toBe(true);
    const canonical = body.connection_url;
    expect(typeof canonical === "string").toBe(true);
    const prefix = expectedBase!.replace(/\/+$/, "") + "/mcp/private_";
    expect(canonical.startsWith(prefix)).toBe(true);
    expect(canonical.slice(prefix.length).includes("/")).toBe(false);
    // The UI must display the server-returned URL, even when it differs from UI origin.
    await expect.poll(async () => (await page.locator("body").innerText()).includes(canonical)).toBe(true);
    const headers = { Accept: "application/json, text/event-stream" };
    const data = { jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "podman-e2e", version: "1" } } };
    const accepted = await anonymous.post(canonical, { headers, data }).catch(() => {
      throw new Error("External MCP initialization failed (URL redacted)");
    });
    expect(accepted.status()).toBe(200);
    const response = await page.request.delete(`${profileApi}/${encodeURIComponent(profileId!)}`);
    expect(response.status()).toBe(204);
    const rejected = await anonymous.post(canonical, { headers, data }).catch(() => {
      throw new Error("External MCP revocation check failed (URL redacted)");
    });
    expect([401, 403, 404].includes(rejected.status())).toBe(true);
    await page.reload();
    const profiles = await page.request.get(profileApi!);
    const row = (await profiles.json()).profiles.find((p: { id: string }) => p.id === profileId);
    expect(row?.active === false && row?.revoked_at != null).toBe(true);
  } finally {
    if (profileId && profileApi) {
      await page.request.delete(`${profileApi}/${encodeURIComponent(profileId)}`);
    }
    await anonymous.dispose();
  }
});
