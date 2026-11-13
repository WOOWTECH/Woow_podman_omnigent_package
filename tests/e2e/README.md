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
OMNIGENT_CHAT_PASSWORD="$(podman secret inspect --showsecret \
    --format '{{.SecretData}}' omnigent-runner-password)" \
  npm test
```

Environment variables:

| var                        | required | default |
| -------------------------- | -------- | ------- |
| `OMNIGENT_BASE_URL`        | yes      | —       |
| `OMNIGENT_ADMIN_PASSWORD`  | yes      | —       |
| `OMNIGENT_ADMIN_USERNAME`  | no       | `admin` (`OMNIGENT_ADMIN_USERNAME` in `~/.config/omnigent/omnigent.env`) |
| `OMNIGENT_CHAT_PASSWORD`   | chat spec | Runner owner's password (`omnigent-runner-password` secret) |
| `OMNIGENT_CHAT_USERNAME`   | no       | `operator` |

Pass the admin and runner-owner passwords from their Podman secrets as above;
do not paste their values into shell history.

Run the chat spec as the runner owner after opening `podman exec -it omnigent-runner pi`
and completing `/login` in its interactive UI:

```
OMNIGENT_BASE_URL=http://127.0.0.1:8000 \
OMNIGENT_CHAT_PASSWORD="$(podman secret inspect --showsecret \
    --format '{{.SecretData}}' omnigent-runner-password)" \
  npx playwright test specs/chat.spec.ts
```

The model test requires Pi to be enabled on the connected runner. It checks a
committed `assistant` message in the session API and its rendered text; seeing the
marker in the user's prompt cannot pass it.

## Suites

- `specs/smoke.spec.ts` — unauthenticated health/info + login flow
- `specs/settings.spec.ts` — settings shell (account/appearance/git/shortcuts/members/policies/sharing/archived)
- `specs/chat.spec.ts` — operator-owned Pi round-trip (archives its session), harness picker,
  Ctrl+Shift+O (serial)
- `specs/inbox-automations.spec.ts` — inbox empty state + automations New automation modal
- `specs/rwd.spec.ts` — 375x812 mobile hamburger + 1440x900 desktop shell

## Notes

- Chromium-only. arm64/amd64 CI friendly.
- `workers: 1` — the sidebar recent-session list races if two tests create sessions
  in parallel.
- No mock/fixture data. Tests hit the real server. The `chat.spec.ts` model
  round-trip still requires pi credentials and needs to be validated separately.
  It archives the session it opened, whether it passed or not, so runs against the
  live deployment do not pile up in the runner owner's session list; Settings >
  Archived sessions keeps them.
- One known-issue marker: `@known-issue omnigent-server-healthcheck-shadowed` on the
  `/healthz` test — the endpoint currently returns SPA HTML instead of JSON; we only
  assert status 200 until the server route is un-shadowed.

## Provenance

These tests were generated from a chrome-devtools MCP walk on **2026-08-31** against
Omnigent **0.11.0** + our runner (pi-native harness) and validated against session
`0a262a34423647e88330a41377f2a78f` (test prompt `e2e-ultracode-2026`, pi replied via
GPT-5.6 Sol). The deployment now pins server **v0.21.0**. On 2026-11-12 the smoke,
settings, automations, inbox, and responsive-layout specs (21 tests), and the chat
spec's harness-picker and shortcut tests, passed against a throwaway 0.21.0 stack
migrated from 0.19.1; the automations spec also passed as a user who had never seen
the automations tour. The chat spec's Pi round-trip needs a pi login and a real model,
so it runs against the live deployment; it passed there on 2026-11-13. The same day all
24 tests passed on a throwaway 0.21.0 stack whose Pi used a stub OpenAI-compatible
model, and the round-trip's session ended up archived whether the reply came or not.
