# Initial package — Omnigent server + Postgres + always-on pi runner

Status: shipped, main HEAD 2026-08-30.

## Motivation

.197 already hosts pi-web (`Woow_podman_pi_agent_package`), Open Design
(`Woow_podman_opendesign`), and code-server
(`Woow_podman_code_server_package`) — three peers that share pi state
via the `pi-agent-data` podman volume. Omnigent is the fourth
consumer: a meta-harness with a first-class Pi harness that ought to
see the same providers, skills, and session history as the rest.

Omnigent's runtime shape is different from the other three, though —
its "server" doesn't spawn pi directly; a separate "runner" process
does. So the addon needs three containers instead of one: postgres +
server + runner. The runner is what mounts `pi-agent-data` and shells
out to pi through the HOME-rescoping `pi-code` wrapper.

## Decisions (grilled with Claude 2026-08-30)

1. **Runner deployment shape**: always-on sidecar on `.197`, not
   per-user. Users open the web UI and pi is available immediately, with
   provider tokens / skills already loaded from the shared volume. The
   trade-off is that one runner serves one active session at a time —
   fine for team-of-few, revisit if we hit concurrency limits.
2. **Postgres**: own container (`postgres:16-alpine`), own volume. Not
   sharing with `hermes-postgresql` (v17.6, different tenant, coupling
   fault domains isn't worth saving one container).
3. **Auth boundary**: `code-server --cert`-style self-signed HTTPS
   doesn't work for webviews (proved on 2026-08-30 with the code-server
   package). Omnigent's UI is plain React, not VS Code webviews, so
   plain HTTP would work — but we still use Tailscale `tailscale serve
   --https=9444` for a real Let's Encrypt cert on the tailnet MagicDNS
   name, matching the code-server / OD pattern.
4. **New repo, not merged into a sibling.** Package naming stays
   consistent (`Woow_podman_<sw>_package`).
5. **Server = upstream ghcr image, no local build.** Only the runner is
   ours to build; CI matrix is amd64 + arm64 → ghcr.
6. **Runner image bundles only pi.** Claude Code / Codex / OpenCode
   integrations are known Omnigent harnesses but not required for the
   "pi to omnigent" goal; a `-full` variant can come later if we want
   multi-harness in one runner.
7. **Runner command is TBD.** README v0.11.0 documents `omnigent run
   <task>` (one-shot) and `omnigent server` (starts a server) but does
   not spell out the long-lived dial-in runner CLI. Initial deployment
   uses a `omnigent-runner-loop` wrapper that logs environment + waits
   for the server + sleeps forever so we can `podman exec` in and
   discover the actual command. Iterate the wrapper live once found.

## Files

```
Containerfile.runner              debian-slim + Node 22 + pi 0.83.0 + pi-code wrapper + omnigent 0.11.0
quadlet/
  omnigent.network                private podman bridge shared by 3 containers
  omnigent-postgres.container     postgres:16-alpine, unpublished, healthcheck via pg_isready
  omnigent-server.container       upstream ghcr image, publishes 127.0.0.1:8000
  omnigent-runner.container       our runner image, dials into server, UserNS keep-id
rootfs/
  usr/local/bin/pi-code           HOME-rescoping wrapper (byte-identical to code-server's)
  usr/local/bin/omnigent-runner-loop  entrypoint: log + wait-for-server + sleep infinity
systemd/
  omnigent-server-health.{service,timer}    30s podman-healthcheck refresh
scripts/
  install.sh                      build runner + install units + start (order: pg → server → runner)
  uninstall.sh                    down; --purge removes omnigent-{server,postgres}-data
tests/
  smoke-container.sh              3 containers up + server /healthz 200 + pg_isready
  smoke-pi-integration.sh         runner has pi 0.83.0 + wrapper + /data/pi-agent visible
  smoke-runner-dialin.sh          runner can reach server over private network
.github/workflows/build.yml       amd64+arm64 CI, ghcr on main + release
```

## Migration path when we figure out the runner CLI

1. `podman exec -it omnigent-runner bash`
2. `omnigent --help` → find the long-lived-runner subcommand
3. Edit `rootfs/usr/local/bin/omnigent-runner-loop`, replace the
   `exec sleep infinity` line with the discovered subcommand invocation
4. `podman build ... && systemctl --user restart omnigent-runner`
5. Update `smoke-runner-dialin.sh` to assert the runner has registered
   (whatever the endpoint / status field turns out to be)

## Access URLs

- Loopback (from .197 itself): `http://127.0.0.1:8000/`
- Tailnet (real HTTPS cert):
  `https://woow-openclaw-services-1.tailb7a69b.ts.net:9444/`
  after: `podman exec woow-tailscale-gateway tailscale serve --bg --https=9444 http://127.0.0.1:8000`
- LAN direct: not exposed. `PublishPort` is loopback-only.

## Rollback

`./scripts/uninstall.sh` removes the three containers + the network
unit + health timer. `pi-agent-data` is external and never touched.
`--purge` also drops the omnigent-{server,postgres}-data volumes if
you want a clean slate on next install.
