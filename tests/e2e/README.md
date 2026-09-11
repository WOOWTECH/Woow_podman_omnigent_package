# Omnigent Web UI — Playwright E2E

Self-contained Playwright + TypeScript E2E suite for the Omnigent Web UI served by the
`Woow_podman_omnigent_package` deployment. It runs against a **real** deployment and carries
no credentials of its own.

## Run

```
cd tests/e2e
npm ci
npm run install-browsers
OMNIGENT_BASE_URL=http://127.0.0.1:8000 \
OMNIGENT_ADMIN_PASSWORD="$(podman secret inspect --showsecret \
    --format '{{.SecretData}}' omnigent-admin-password)" \
  npm test
```

Environment variables:

| var                        | required | default |
| -------------------------- | -------- | ------- |
| `OMNIGENT_BASE_URL`        | yes      | —       |
| `OMNIGENT_ADMIN_PASSWORD`  | yes      | —       |
| `OMNIGENT_ADMIN_USERNAME`  | no       | `admin` (`OMNIGENT_ADMIN_USERNAME` in `~/.config/omnigent/omnigent.env`) |

The password is the deployment's admin credential: pass it in from the podman secret as
above, and do not paste it into a shell that records history.

## Suites

- `specs/smoke.spec.ts` — unauthenticated health/info + login flow
- `specs/settings.spec.ts` — settings shell (account/appearance/git/shortcuts/members/policies/sharing/archived)
- `specs/chat.spec.ts` — new session round-trip, harness picker, Ctrl+N (serial)
- `specs/inbox-automations.spec.ts` — inbox empty state + automations New task modal
- `specs/rwd.spec.ts` — 375x812 mobile hamburger + 1440x900 desktop shell

## Notes

- Chromium-only. arm64/amd64 CI friendly.
- `workers: 1` — the sidebar recent-session list races if two tests create sessions
  in parallel.
- No mock/fixture data. Tests hit the real server, including a real Pi model
  round-trip in `chat.spec.ts` (allowed up to 90s).
- One known-issue marker: `@known-issue omnigent-server-healthcheck-shadowed` on the
  `/healthz` test — the endpoint currently returns SPA HTML instead of JSON; we only
  assert status 200 until the server route is un-shadowed.

## Provenance

These tests were generated from a chrome-devtools MCP walk on **2026-08-31** against
Omnigent **0.11.0** + our runner (pi-native harness) and validated against session
`0a262a34423647e88330a41377f2a78f` (test prompt `e2e-ultracode-2026`, pi replied via
GPT-5.6 Sol). The deployment now pins server **v0.12.0**; a few selectors may need a
refresh on the next run.
