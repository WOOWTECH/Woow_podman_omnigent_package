# Woow Podman Omnigent

[![Podman](https://img.shields.io/badge/Podman-%E2%89%A54.4%20rootless-892CA0)](https://podman.io)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![Omnigent](https://img.shields.io/badge/omnigent-0.11.0-blueviolet)](https://github.com/omnigent-ai/omnigent)
[![pi-coding-agent](https://img.shields.io/badge/pi--coding--agent-0.83.0-blue)](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**English** · [繁體中文](README_zh-TW.md)

[Omnigent](https://github.com/omnigent-ai/omnigent) — the open-source
meta-harness — packaged for rootless Podman as three cooperating
containers: **Postgres**, the upstream **server**, and an **always-on
runner** that mounts the shared `pi-agent-data` volume and exposes the
`pi` harness. Sibling to [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package),
[`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign),
and [`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package) —
one pi state, four consumers.

---

## What you get

| | |
|---|---|
| **Web UI** | `http://127.0.0.1:8000/` (loopback); real-HTTPS on tailnet via `tailscale serve --https=9444` |
| **Server** | Upstream `ghcr.io/omnigent-ai/omnigent-server:latest` — no local build, tracks upstream releases |
| **Database** | Own `postgres:16-alpine` container, own volume, unpublished |
| **Runner** | Always-on sidecar with pi 0.83.0 + `pi-code` wrapper + `OMNIGENT_PI_PATH` wired at the shared `/data/pi-agent` volume |
| **Auth** | Upstream built-in accounts flow (first-boot username+password bootstrap), see [First boot](#first-boot) |
| **Supervision** | `systemd --user` Quadlet units with 30 s health-refresh timer on the server |

---

## Why a sidecar runner?

Omnigent's architecture is not the same as code-server's or OD's. The
server orchestrates and hosts the web UI, but agent execution happens
in a separate **runner** process that dials in over WebSocket. Two
paths:

1. Users run `omnigent run --harness pi` on their own laptop — they
   get pi with their own state.
2. Or, this addon: a runner runs on `.197` permanently, mounts the
   shared `pi-agent-data` volume, and every session started from the
   web UI can pick "pi" as its harness with pi-web's providers and
   skills already loaded.

We chose (2) — it keeps Omnigent's pi experience consistent with
pi-web, OD, and code-server, all of which read/write the same volume
via the `pi-code` HOME-rescoping wrapper.

Full decision log in [`docs/plans/2026-08-30-initial-package.md`](docs/plans/2026-08-30-initial-package.md).

---

## Install

Requires Podman ≥ 4.4 (Quadlet), rootless. Highly recommended: install
[`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package)
first so the shared `pi-agent-data` volume has providers/skills already;
otherwise this repo creates an empty one and you must configure a
provider from scratch.

```bash
git clone https://github.com/WOOWTECH/Woow_podman_omnigent_package.git
cd Woow_podman_omnigent_package
./scripts/install.sh
```

Startup order: `omnigent-postgres` → `omnigent-server` → `omnigent-runner`.
The install script waits for the server to answer `/health` (JSON
`{"status":"ok"}`) before returning, then **auto-claims the first admin**
via `POST /auth/setup` using the `OMNIGENT_ADMIN_{USERNAME,PASSWORD}`
env in `quadlet/omnigent-runner.container` — no manual `/setup` form
click, and the runner container's later `/auth/login` lands on a db
that already has that admin. On a system where admin already exists
the setup POST returns 409 and is silently skipped.

**Do not swap `/healthz` in as a liveness probe** — the React SPA
catch-all serves `index.html` for that path with HTTP 200 even when
the API is dead, so probes never fail.

**Change the default admin credentials** before publishing this stack
to anyone outside the trust boundary — the defaults live in
`quadlet/omnigent-runner.container` and are the single source of truth
for both the runner's dial-in login and the human admin.

Skip the runner image rebuild with `OD_SKIP_BUILD=1 ./scripts/install.sh`.

## First boot

The upstream server does **not** auto-generate an admin password. On
first boot, `podman logs omnigent-server` prints a `"No admin yet"`
line pointing at the base URL — open that URL, fill in the Create-admin
form (username + password).

To pre-seed the password so there is no race window where the first
visitor claims the instance, uncomment
`OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` in
`quadlet/omnigent-server.container` **before** running `install.sh`.

## Tailnet HTTPS

Omnigent's web UI is a plain React SPA (no ServiceWorker webviews), so
plain HTTP over LAN would work — but consistency with the code-server
package's HTTPS story is easy:

```bash
podman exec woow-tailscale-gateway \
    tailscale serve --bg --https=9444 http://127.0.0.1:8000
```

Then open **`https://woow-openclaw-services-1.tailb7a69b.ts.net:9444/`** —
real Let's Encrypt cert issued via Tailscale's API, browsers trust it
automatically, no self-signed prompt.

## Uninstall

```bash
./scripts/uninstall.sh          # stops + removes; keeps omnigent-{server,postgres}-data
./scripts/uninstall.sh --purge  # also deletes those data volumes
```

`pi-agent-data` is **not** touched (owned by the sibling pi-web package).

---

## Layout

```
Containerfile.runner         Debian 12 slim + Node 22 + pi 0.83.0 + omnigent 0.11.0 + pi-code
quadlet/
  omnigent.network           private podman bridge shared by 3 containers
  omnigent-postgres.container    postgres:16-alpine, unpublished, pg_isready healthcheck
  omnigent-server.container      upstream ghcr image, publishes 127.0.0.1:8000
  omnigent-runner.container      our runner image, dials into server, UserNS keep-id
rootfs/
  usr/local/bin/pi-code              HOME-rescoping wrapper (same as code-server ships)
  usr/local/bin/omnigent-runner-loop long-lived runner entrypoint (iterate live)
systemd/
  omnigent-server-health.{service,timer}   30 s podman healthcheck refresh
scripts/
  install.sh                 build runner + install units + start in dependency order
  uninstall.sh               down; --purge deletes data volumes
tests/
  smoke-container.sh         3 containers up + server /health + pg_isready
  smoke-pi-integration.sh    pi 0.83.0 + pi-code + /data/pi-agent visible + env
  smoke-runner-dialin.sh     runner can reach server over private network
docs/plans/                  design decisions for the changes shaping this package
.github/workflows/build.yml  amd64 + arm64 CI, ghcr on main + release
```

---

## Verifying a deployment

```bash
bash tests/smoke-container.sh          # 3 up, /health 200 JSON, pg_isready
bash tests/smoke-pi-integration.sh     # pi + wrapper + volume + env
bash tests/smoke-runner-dialin.sh      # runner -> server reachable
```

All three green on a healthy deployment. `smoke-pi-integration.sh` `skip`s
(does not `fail`) the sibling-artefact checks if `pi-agent-data` was
freshly created empty.

---

## Operating

```bash
podman ps --format '{{.Names}}\t{{.Status}}'
journalctl --user -u omnigent-server -f
podman logs -f omnigent-server         # web UI + orchestration events
podman logs -f omnigent-postgres       # db
podman logs -f omnigent-runner         # agent runner status
podman exec -it omnigent-runner bash   # shell inside the runner (find the real runner CLI here)
systemctl --user restart omnigent-server  # server-only restart
```

To bump versions:
- **Server**: edit `Image=` in `quadlet/omnigent-server.container` (pin
  a `sha-<short>` or `vX.Y.Z` tag) → `systemctl --user restart omnigent-server`
- **Runner (pi / omnigent)**: bump ARG in `Containerfile.runner`,
  `./scripts/install.sh` (auto-rebuilds), `systemctl --user restart omnigent-runner`

pi CLI version must stay in lockstep with the sibling
`Woow_podman_pi_agent_package` — the shared volume's on-disk schema is
not versioned.

---

## Security posture

- **Loopback publish** for the server — the outward door is Tailscale
  HTTPS, not direct LAN.
- **Postgres unpublished** — reachable only from the omnigent podman
  network (which only the 3 sibling containers join here).
- **UserNS keep-id** on the runner so it can read/write the shared
  volume as host uid 1000 without a chown dance. Same pattern as
  code-server.
- **`OMNIGENT_AUTH_ENABLED=1`** — multi-user built-in accounts. Do NOT
  set to `0` if this deployment is reachable past a fully-trusted LAN.
- **First-boot race**: while the admin roster is empty, `POST
  /auth/setup` is unauthenticated. Either pre-seed
  `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`, or open the URL yourself
  before anyone else can.

---

## Related packages

- [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package) — the pi-web sibling that owns the shared `pi-agent-data` volume
- [`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign) — OpenDesign with the same volume-share pattern
- [`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package) — code-server + ACP Client extension with the same pi-code wrapper
- [Omnigent (upstream)](https://github.com/omnigent-ai/omnigent) — Apache 2.0 meta-harness

## License

MIT
