# Changelog

Versions are `<omnigent version>-<package revision>`, the same string as `VERSION` and the tag
of the runner image.

## 0.21.0-2 - 2026-11-14

### Fixed

- **Settings → Sharing now survives a restart.** The server gets
  `OMNIGENT_ADMIN_CREDENTIALS_PATH=/data/admin-credentials`, as in upstream's compose file and
  the HA add-on. Upstream only uses the directory of that path (no credentials file is
  written): it holds the overrides an admin sets in Settings → Sharing (sharing mode, public
  access, the most a public link may do, which new sessions start public) and the optional
  `admins` roster. Without it they went to `/root/.omnigent` in the container, which every
  restart and upgrade recreates empty, so the page quietly fell back to the unit's defaults.
  They are now on `omnigent-server-data`, which `scripts/backup.sh` exports.

### Changed

- **Refresh grants last 365 days** (`OMNIGENT_GRANT_MAX_LIFETIME_DAYS`, was 30), the WOOW PaaS
  value. The runner and `omnigent login` sign in again a year after a grant was approved
  instead of a month. The limit is checked against each grant's approval time, so it also
  applies to grants issued before the upgrade.

### Notes

- The upgrade restarts the server once, and the runner for its new image tag. A Settings →
  Sharing change made before this revision does not survive that restart, as before.
- An override wins over the unit's default: public access turned on in Settings → Sharing now
  stays on, and the e2e settings spec fails until it is turned off again.

## 0.21.0-1 - 2026-11-12

### Changed

- **Omnigent 0.19.1 -> 0.21.0**: server image `v0.21.0` (tag and digest), `omnigent==0.21.0` in
  the runner image. This is the version the WOOW PaaS server and the HA add-on (0.1.20) run.
  The schema migrates forward on the first start through five additive revisions; take the
  dump first, there is no way back without it. Rehearsed on a copy of a live 0.19.1 database
  and on a seeded stack (see README, "From 0.19.1 to 0.21.0").
- The e2e suite follows the 0.21.0 web UI: it dismisses the "Introducing Automations" tour
  that opens on a user's first visit, checks the New automation form's Session, Host and
  Harness fields (they replaced "Runs with"), reads the sharing modes from the select that
  replaced the radios and checks that public access is off, and picks the sidebar's main
  "New session" entry now that the Sessions header has a second one.

### Notes

- pi stays at 0.85.1; 0.21.0 still requires 0.84.2 or later. The 0.21.0 breaking changes
  (OpenCode 2.0, the desktop app on macOS 13+) do not apply: the runner offers only pi.
- The Pi provider readiness helper (`omnigent-runner-configure-pi`, #10) was merged without a
  revision bump, so `0.19.1-2` images built before and after it differ. `0.21.0-1` always
  has it.

## 0.19.1-2 - 2026-11-08

- Pre-seed the administrator inside the server before serving requests. Install no longer
  calls the unauthenticated `/auth/setup` route.
- Create a daily non-admin operator through the accounts invite/register API on first boot.
  The runner logs in as this account and receives only its password secret. An existing
  operator is checked for matching credentials and non-admin status on every install.
- Disable public sharing and set refresh-grant lifetime to 30 days. The server and runner
  drop all Linux capabilities; Postgres retains only the five needed by its entrypoint.
- Rotate admin and operator passwords through `/auth/users/me/password`; back up and restore
  the operator secret with the other secrets. Bump the runner image revision.

## 0.19.1-1 - 2026-11-07

### Changed

- **Omnigent 0.12.0 -> 0.19.1**: server image `v0.19.1`, `omnigent==0.19.1` in the runner
  image. The schema migrates forward on the first start; a 0.12.0 database with an admin, a
  second account and a session came up intact on a throwaway stack (see README, "From 0.12.0
  to 0.19.1"). Take the dump first: there is no way back without it.
- The server and Postgres images are pinned by tag **and digest** (`tag@sha256:...`).
- The runner logs in the way `omnigent login` does (`POST /auth/login` with `issue_refresh`,
  stored with `omnigent.cli_auth.store_token`) instead of hand-writing
  `~/.omnigent/auth_tokens.json`. A hand-written entry has no refresh grant, so the host daemon
  would go offline when its 8 hour token lapsed.

### Added

- `rootfs/usr/local/bin/omnigent-runner-login`: logs in, and with `--ensure` renews the access
  token from the refresh grant or logs in again when it cannot.
- A watchdog in `omnigent-runner-loop` (every `OMNIGENT_RUNNER_CHECK_INTERVAL` seconds, 60 by
  default) that keeps the login fresh and starts the host daemon again when its process is
  gone; the log tail follows the restarted daemon's new log file.

- `tests/runner-login.sh` (31 checks, no container): the login helper against a fake auth
  server and the loop's watchdog against stub CLI commands, run by `scripts-ci`. The CI job also
  lints `omnigent-runner-loop`, which the vendored shellcheck step does not reach.

### Notes

- A host belongs to the account that registered it (`GET /v1/hosts` lists only the caller's,
  launching on another account's host is a 403). The runner still logs in with the account
  named by `OMNIGENT_ADMIN_USERNAME`.
