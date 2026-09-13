# Woow Podman Omnigent

[![Podman](https://img.shields.io/badge/Podman-%E2%89%A54.9%20rootless-892CA0)](https://podman.io)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![Omnigent](https://img.shields.io/badge/omnigent-0.12.0-blueviolet)](https://github.com/omnigent-ai/omnigent)
[![pi-coding-agent](https://img.shields.io/badge/pi--coding--agent-0.85.1-blue)](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**English** · [繁體中文](README_zh-TW.md)

[Omnigent](https://github.com/omnigent-ai/omnigent), the open-source meta-harness, packaged
for rootless Podman as three cooperating containers supervised by systemd: **Postgres**, the
upstream **server**, and an **always-on runner** that offers the `pi` harness to every
session started from the web UI.

> ### Rotate the credentials that were committed
>
> Until this change, `quadlet/omnigent-postgres.container`, `-server.container` and
> `-runner.container` carried a database password and an admin username/password in plain
> text, and `install.sh` claimed that admin on first boot. **Those values are in the git
> history of a public repository and cannot be removed from it.** Any host that ran them
> must rotate both, now:
>
> ```bash
> scripts/rotate-secrets.sh --all      # new database password; new admin password after you
>                                      # change it in the web UI (Settings -> Account)
> ```
>
> Treat any other system that used the same strings as exposed too. New installs generate
> their own credentials as podman secrets and never write them to disk in the clear.

---

## What you get

| | |
|---|---|
| **Web UI** | `http://127.0.0.1:8000/` by default; the front door is a tailnet or tunnel on the same host |
| **Server** | Upstream `ghcr.io/omnigent-ai/omnigent-server:v0.12.0`, pinned; no local build |
| **Database** | Its own `postgres:16.15-alpine3.24` container and volume, never published |
| **Runner** | Always-on sidecar with pi 0.85.1 and the `pi-code` wrapper, wired to `/data/pi-agent` |
| **Secrets** | podman secrets generated at install: database password (file-mounted), `DATABASE_URL`, admin password |
| **Supervision** | `systemd --user` Quadlet units, a real readiness gate on Postgres, a 30 s health-refresh timer |

---

## Install

Rootless podman >= 4.9 (tested on 4.9.3) with systemd 255 and linger, plus `curl` and `jq`.

```bash
git clone https://github.com/WOOWTECH/Woow_podman_omnigent_package.git
cd Woow_podman_omnigent_package
scripts/install.sh                       # or: scripts/install.sh --port 18000
```

`scripts/install.sh` is idempotent. It:

1. checks the host (not root, podman >= 4.9, the Quadlet generator, a reachable
   `systemctl --user`) and enables linger;
2. creates `~/.config/omnigent/omnigent.env` (mode 0600) from
   [`config/omnigent.env.example`](config/omnigent.env.example) on the first run;
   `--port N`, `--bind ADDR`, `--pi-state MODE` and `--set KEY=VALUE` change a setting and
   save it there;
3. refuses to continue when a container named `omnigent-postgres`, `omnigent-server` or
   `omnigent-runner` exists that Quadlet does not manage (Quadlet starts containers with
   `podman run --replace`), when the chosen port is taken, or when an
   `omnigent-postgres-data` volume exists without the matching password secret;
4. renders [`quadlet/`](quadlet/) and [`systemd/`](systemd/) with the env file (`@@VAR@@`
   tokens, whitelist in `quadlet/render-vars`) and checks the result with the podman 4.9.3
   generator and `systemd-analyze --user verify` **before** anything is installed;
5. builds `localhost/woow-omnigent-runner:<VERSION>` when that tag is missing (`--rebuild`
   forces it, `--no-build` forbids it) and pre-pulls the pinned server and Postgres images;
6. creates the podman secrets if they are missing and always re-derives `DATABASE_URL` from
   the database password, so the two cannot drift;
7. installs only the files that changed and restarts only the units whose files changed;
8. waits for `/health`, **claims the first admin** through `POST /auth/setup` with the
   generated password (piped from the secret, never in a command line), waits for both
   containers to be healthy and runs [`tests/smoke.sh`](tests/smoke.sh).

`scripts/install.sh --dry-run` renders and validates everything and reports what it would
change, without changing anything.

### First login

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password
```

Open the web UI (over your tailnet/tunnel, or `ssh -L 8000:127.0.0.1:8000 <host>` and
<http://localhost:8000>) and log in as the `OMNIGENT_ADMIN_USERNAME` from the env file.
In private pi mode, give the runner its own pi login once:

```bash
podman exec -it omnigent-runner pi login
```

### Settings

Edit `~/.config/omnigent/omnigent.env` and re-run `scripts/install.sh`.

| Key | Default | Notes |
|-----|---------|-------|
| `OMNIGENT_BIND` | `127.0.0.1` | Publish address; a LAN IP is allowed, `0.0.0.0` is refused. |
| `OMNIGENT_PORT` | `8000` | Host port of the web UI. |
| `OMNIGENT_ACCOUNTS_BASE_URL` | *(empty)* | Public/tailnet URL for invite links and OAuth redirects. Empty lets upstream derive it. |
| `OMNIGENT_ADMIN_USERNAME` | `admin` | The account install.sh claims and the runner logs in with. |
| `OMNIGENT_PI_STATE` | `private` | `private` (own volume) or `shared` (pi-web's `pi-agent-data`). |

### pi state: private or shared

- **private** (default): the runner gets `omnigent-pi-data`, a volume this package owns. It
  needs one `pi login` inside the runner, it is backed up by `scripts/backup.sh`, and
  `uninstall.sh --purge` deletes it. Nothing is shared with pi-web, so there is no pi
  version skew.
- **shared** (`--pi-state shared`): the runner mounts **`pi-agent-data`**, the volume owned
  by [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package),
  and sees pi-web's provider logins, sessions and skills. The runner unit then *references*
  that package's `pi-agent-data.volume` unit, so it gets a real
  `Requires=pi-agent-data-volume.service` and install.sh refuses the mode unless that unit is
  installed (a dangling reference would silently become an empty `systemd-pi-agent-data`
  volume on podman 4.9.3). This package never installs, labels, backs up or deletes
  `pi-agent-data`: **`--purge` cannot reach it.** Keep the pi versions of both deployments in
  step; the on-disk format is not versioned.

### Startup order

`omnigent-network` and the volume units come first, then `omnigent-postgres`, which only
becomes *active* once its `ExecStartPost=` gate sees Postgres accept TCP connections (podman
4.9.3 ignores `Notify=healthy`, so this gate is what makes `Requires=`/`After=` mean
anything), then `omnigent-server`, then `omnigent-runner`.

---

## Secrets

| podman secret | Carries | How the container gets it |
|---|---|---|
| `omnigent-postgres-password` | the database role password | `type=mount` + `POSTGRES_PASSWORD_FILE`, so it is not in `podman inspect` |
| `omnigent-database-url` | `postgresql+psycopg://omnigent:<password>@omnigent-postgres:5432/omnigent` | `type=env DATABASE_URL` on the server |
| `omnigent-admin-password` | the admin account password | `type=env OMNIGENT_ADMIN_PASSWORD` on the runner |

All three are generated from `/dev/urandom` at install time, created through a pipe (never in
argv, logs or xtrace) and never overwritten by `install.sh`. The env file holds no passwords.

**Caveat (podman 4.9.3, verified):** a `type=env` secret *is* visible in `podman inspect` of
the running container, so `DATABASE_URL` and the admin password can be read by anything that
can use this user's podman socket — including a podman MCP server, if one runs here. They
stay out of git, the unit files, `systemctl --user cat`, the create command and the journal.
The database password itself is file-mounted and not exposed that way.

Rotation:

```bash
scripts/rotate-secrets.sh --db       # ALTER ROLE + both database secrets + restart the server
scripts/rotate-secrets.sh --admin    # after changing the password in the web UI
scripts/rotate-secrets.sh --all
```

---

## Tailnet HTTPS

The web UI is a plain React SPA, but a browser-trusted origin is still the right front door.
On whichever tailscale node this host runs (the WOOWTECH one is the `woow-tailscale`
container):

```bash
podman exec woow-tailscale tailscale serve --bg --https=9444 http://127.0.0.1:8000
```

Then set `OMNIGENT_ACCOUNTS_BASE_URL=https://<node>.<tailnet>.ts.net:9444` in the env file and
re-run `scripts/install.sh`, so invite links and redirects point at the name people use.

---

## Upgrade

```bash
git pull
scripts/upgrade.sh
```

It snapshots the installed units, runs `scripts/backup.sh` (pg_dump plus volume exports),
runs `scripts/install.sh` and `tests/smoke.sh`, and on any failure puts the previous units
back and restarts them on the previous image tags. Upstream's database migrations are
one-way: if the new server migrated the schema, the rollback also needs the dump, and the
script prints the exact `scripts/restore.sh` command for it.

Bumping the server means bumping `Image=` in `quadlet/omnigent-server.container`,
`ARG OMNIGENT_VERSION` in `Containerfile.runner` and `VERSION` together; `tests/dryrun.sh`
fails when they disagree.

## Backup and restore

```bash
scripts/backup.sh                     # -> ~/backups/omnigent/<timestamp>/
scripts/backup.sh --include-secrets   # also the database and admin passwords (secrets.env, 0600)
scripts/backup.sh --stop              # stop the runner and the server around the volume exports
scripts/restore.sh ~/backups/omnigent/<timestamp> [--with-secrets]
```

The backup is a `pg_dump -Fc` of the database, an export of `omnigent-server-data`, an export
of `omnigent-pi-data` in private mode, and a copy of the env file. `restore.sh` stops the
runner and the server, drops and recreates the database from the dump, replaces the volumes
and runs the smoke test. `pi-agent-data` (shared mode) belongs to the pi-web package and is
neither backed up nor restored here.

## Uninstall

```bash
scripts/uninstall.sh                   # stop and remove the units; keep volumes, secrets, images, env file
scripts/uninstall.sh --purge           # also delete the omnigent volumes, network and secrets
scripts/uninstall.sh --purge-images    # also remove the localhost/woow-omnigent-runner:* images
```

`--purge` is the only way these scripts delete data. It takes a final backup first and asks
you to type the app name (`--yes` skips the question). It never touches `pi-agent-data`.

## Converging a hand-edited install

woowtechopenclaw already runs omnigent as Quadlet - but from units that were written by hand
and then edited in place. The container names, the volumes (`omnigent-postgres-data`,
`omnigent-server-data`) and the network (`omnigent`) are already the ones this repo declares,
so there is **nothing to migrate**: no legacy container to rename or capture, no data to adopt
by any other name. This repo therefore ships no `migrate-legacy.sh`. What that host needs is a
**converge**, and the converge is `scripts/install.sh`: `ql_install_files` keeps a copy of every
foreign file with our name before writing ours, and `ql_apply_units` restarts only the units
whose file actually changed. It is the same path `Woow_podman_pi_agent_package` took on
toypark1234, where repointing a hand-edited `pi-web.container` at `%h`/`%t` cost 1.5 s.

```bash
scripts/converge.sh --check      # pre-flight + drift report + install.sh --dry-run
scripts/converge.sh              # backup, adopt the secrets, install.sh, verify, report
scripts/converge.sh              # again: "files changed : none", no downtime
scripts/converge.sh --status
scripts/converge.sh --rollback   # the previous unit files back, restarted on the old images
```

`scripts/converge.sh` never installs anything itself. It wraps that one `install.sh` call with
the evidence an operator needs around a live service:

**A pre-flight that refuses instead of guessing.** All three containers must exist, be running
and carry `PODMAN_SYSTEMD_UNIT=<name>.service`; anything else is a migration and is refused.
Both volumes must exist. The publish address, the port, the base URL, the admin username and
the pi-state mode are read off the running containers, never defaulted from this repo.

**A drift report, per file, before anything restarts.** On openclaw it names exactly what the
hand-written units carry that this repo does not: floating tags (`postgres:16-alpine`,
`omnigent-server:latest`, `woow-omnigent-runner:latest`), `AutoUpdate=local`,
`Environment=POSTGRES_PASSWORD=…` and a `DATABASE_URL` with the password in it, no
`SuccessExitStatus=143`, and `Volume=pi-agent-data` as a bare name instead of a `.volume` unit.

**Secret adoption, which is the one thing that cannot be skipped.** The Postgres role's password
was set by `initdb` on the volume being adopted: only a secret holding *that* password opens it.
`converge.sh` reads it out of the running container and pipes it into `podman secret create` -
it never reaches argv, the journal or `set -x`. `install.sh` refuses to run when the volume
exists and the secret does not, on purpose; `converge.sh` is what satisfies that refusal.
`OMNIGENT_ADMIN_PASSWORD` is adopted the same way, so the runner can still log in. `--check`
creates those two secrets as well - they are additive podman secrets derived from what is
already running, and without them `install.sh --dry-run` refuses to render anything, so a
`--check` that skipped them would validate nothing. It still touches no unit file, no
container and no service.

**A backup first, with checksums.** `~/backups/omnigent/converge-<timestamp>/` holds a real
`pg_dump -Fc` (a volume export of a live PGDATA is a torn copy), `pg_dumpall --roles-only`, an
export of both volumes, a copy of every unit file that is about to be overwritten, the
`podman inspect` and a `precheck.txt` - all in `SHA256SUMS`, `0700`/`0600`.

**Proof that the data was adopted.** A `.volume` adopts by *name*; that is only worth trusting
if the name still resolves to the same directory. Each volume's `CreatedAt` and mountpoint inode
are recorded before and asserted after. A mismatch fails the converge and rolls it back.

**A measured downtime.** A prober samples `/health` every 100 ms from outside; the gap between
the last success before the restart and the first one after it is what gets reported.

### `pi-agent-data` is not ours

In `OMNIGENT_PI_STATE=shared` the runner mounts `pi-agent-data`, the volume of
[Woow_podman_pi_agent_package](https://github.com/WOOWTECH/Woow_podman_pi_agent_package), which
`pi-web` - a live service on both hosts - also mounts. This package **references** that volume
unit and never owns it: it ships no `pi-agent-data.volume`, `install.sh` never stages one, and
nothing here starts, restarts, stops or removes `pi-agent-data-volume.service` or
`pi-web.service`. `converge.sh` records `pi-web`'s container id and `StartedAt` and the volume's
identity before and after, and fails the converge if either moved - so "pi-web was not
disturbed" is a measurement in the log, not an assumption. `tests/converge-model.local.sh` pins
all of that.

### Rollback

`scripts/converge.sh --rollback` puts the saved unit files back, reloads and restarts the three
services. Nothing in the converge removes an image, so the old floating tags are still on the
host and the restored units start on exactly what they ran before. The database is untouched
unless you rotated the secrets afterwards; if you did, restore `omnigent.pgdump` from the backup
directory as well.

### Re-running is safe, and it does not move the rollback point

A converge that changed nothing still takes a fresh backup (a `pg_dump` and a volume export
are worth having either way), but only a run that actually replaced a unit file becomes the
`--rollback` target. Otherwise the second, no-op run - the one you are told to make - would
silently replace the saved pre-converge units with the already-converged ones and destroy the
only way back.

### Afterwards

The adopted passwords were in the old unit files - plain text on disk, and in every backup of
them. Rotate with `scripts/rotate-secrets.sh --all`, then run `tests/smoke.sh`.

---

## Layout

```
Containerfile.runner    python 3.12.14 + Node 22 + pi + omnigent + pi-code (pinned bases)
VERSION                 <omnigent version>-<package revision>, the runner image tag
quadlet/                omnigent.network, omnigent-{postgres,server,pi}.volume,
                        omnigent-{postgres,server,runner}.container, render-vars
systemd/                omnigent-server-health.{service,timer}: 30 s health refresh
config/                 omnigent.env.example
rootfs/usr/local/bin/   pi-code (HOME-rescoping wrapper), omnigent-runner-loop
scripts/                install, upgrade, uninstall, backup, restore, rotate-secrets,
                        render-args.sh; lib/quadlet-lib.sh (vendored, checksum-pinned)
scripts/converge.sh     a hand-installed Quadlet host -> these units: pre-flight, backup,
                        secret adoption, adoption proof, measured downtime, --rollback
scripts/converge-lib.sh the drift, backup, restore and downtime helpers it shares with tests/
tests/                  dryrun.sh (+ dryrun.local.sh, fixtures/), smoke.sh,
                        smoke-{container,pi-integration,runner-dialin}.sh, e2e/ (Playwright)
tests/converge-model.sh shim-driven: drift, backup round trip, downtime, and the property
                        that defines a finished converge - the second run changes nothing
tests/shims/            podman and systemctl doubles (no container is created)
```

## Verifying a deployment

```bash
tests/dryrun.sh                    # renders the units and checks them; no containers
tests/smoke.sh                     # units, health, loopback, admin login, runner registration
bash tests/smoke-container.sh      # three containers up, /health, pg_isready
bash tests/smoke-pi-integration.sh # pi version, pi-code wrapper, /data/pi-agent, OMNIGENT_PI_PATH
bash tests/smoke-runner-dialin.sh  # the runner reaches the server over the private network
```

`tests/e2e/` is a Playwright suite for the web UI; it needs `OMNIGENT_BASE_URL` and
`OMNIGENT_ADMIN_PASSWORD` from the environment and carries no credentials of its own.

## Operating

```bash
podman ps --format '{{.Names}}\t{{.Status}}'
journalctl --user -u omnigent-server -f
podman logs -f omnigent-runner                 # login, host registration, harness activity
systemctl --user restart omnigent-server       # the runner follows through Requires=
```

## Security posture

- **Loopback publish** by default; the outward door is a tailnet or tunnel on this host.
- **Postgres is never published** and only reachable on the private omnigent network.
- **No plaintext credentials** in the repo, the units or the journal (see Secrets).
- **`OMNIGENT_AUTH_ENABLED=1`**: the built-in accounts flow. Do not set it to `0` on anything
  reachable beyond this host.
- **First-boot window**: while the admin roster is empty, `POST /auth/setup` is
  unauthenticated. install.sh claims the admin within seconds of the server answering
  `/health`, and the server is on loopback while it happens.
- **UserNS keep-id** on the runner, so pi's files on the volume stay owned by the host user.

## Related packages

- [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package) — pi-web; owns the `pi-agent-data` volume used by shared mode
- [`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package) — code-server + the same `pi-code` wrapper
- [`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign) — OpenDesign
- [Omnigent (upstream)](https://github.com/omnigent-ai/omnigent) — Apache 2.0 meta-harness

## License

MIT
