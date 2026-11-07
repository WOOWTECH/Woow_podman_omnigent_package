# Changelog

Versions are `<omnigent version>-<package revision>`, the same string as `VERSION` and the tag
of the runner image.

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
