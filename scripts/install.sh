#!/usr/bin/env bash
# Install the omnigent stack on a rootless podman host: postgres + server + runner
# as three Quadlet units on a shared omnigent network, plus a systemd
# health-refresh timer for the server.
#
#   ./scripts/install.sh                    # build runner image + install + start all 3
#   OD_SKIP_BUILD=1 ./scripts/install.sh    # keep the current runner image
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUADLET_DIR="${HOME}/.config/containers/systemd"
USER_UNIT_DIR="${HOME}/.config/systemd/user"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Do not run this as root. Rootless podman is the design."
command -v podman >/dev/null || die "podman not found"

say "Checking Podman + Quadlet"
podman --version
GEN=""
for p in /usr/lib/systemd/user-generators/podman-user-generator \
         /usr/libexec/podman/quadlet \
         /usr/lib/systemd/system-generators/podman-system-generator; do
    [ -x "$p" ] && { GEN="$p"; break; }
done
[ -n "${GEN}" ] || die "Quadlet generator not found. Podman >= 4.4 required."

say "Enabling lingering so the stack survives logout"
loginctl enable-linger "$(id -un)" || warn "enable-linger failed"

if ! podman volume exists pi-agent-data 2>/dev/null; then
    warn "External volume pi-agent-data missing — creating an empty one."
    warn "Install Woow_podman_pi_agent_package too if you want a shared"
    warn "pi state (sessions, models, skills) with pi-web / OD / code-server."
    podman volume create pi-agent-data >/dev/null
fi

if [ "${OD_SKIP_BUILD:-0}" != "1" ]; then
    say "Building localhost/woow-omnigent-runner:latest"
    podman build --format=docker -t localhost/woow-omnigent-runner:latest \
        -f "${REPO_DIR}/Containerfile.runner" "${REPO_DIR}"
fi

say "Installing units"
mkdir -p "${QUADLET_DIR}" "${USER_UNIT_DIR}"
for unit in omnigent.network omnigent-postgres.container omnigent-server.container omnigent-runner.container; do
    install -m 0644 "${REPO_DIR}/quadlet/${unit}" "${QUADLET_DIR}/${unit}"
done
for unit in omnigent-server-health.service omnigent-server-health.timer; do
    install -m 0644 "${REPO_DIR}/systemd/${unit}" "${USER_UNIT_DIR}/${unit}"
done

say "Reloading systemd + starting (dependency order: postgres → server → runner)"
systemctl --user daemon-reload
systemctl --user enable --now podman.socket
# Starting the runner also transitively starts server, which transitively
# starts postgres — but doing them in order lets us surface failures at
# the right layer if any of them refuses to come up.
systemctl --user start omnigent-postgres.service
systemctl --user start omnigent-server.service
systemctl --user start omnigent-runner.service
systemctl --user enable --now omnigent-server-health.timer

say "Waiting for omnigent-server /health"
for i in $(seq 1 60); do
    # /health returns JSON {"status":"ok"}. Do NOT use /healthz — the
    # React SPA catch-all serves index.html with HTTP 200 for that path
    # even when the API is dead, so the probe passes forever.
    if curl -sSf -o /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
        say "  ready after ~$((i*2))s"
        break
    fi
    [ "$i" -eq 60 ] && warn "still not ready after 2 min — check: podman logs omnigent-server"
    sleep 2
done

cat <<EOF

$(say "Done")

  UI (loopback)   http://127.0.0.1:8000
  Logs            podman logs -f omnigent-server
                  podman logs -f omnigent-postgres
                  podman logs -f omnigent-runner
  Shell           podman exec -it omnigent-server bash
                  podman exec -it omnigent-runner bash
  Stop            systemctl --user stop omnigent-runner omnigent-server omnigent-postgres
  Status          podman ps --format '{{.Names}}\t{{.Status}}'

  First boot: watch 'podman logs omnigent-server' for a "No admin yet" line,
  open the base URL, and create the first admin (username + password) via
  the Create-admin form. Or pre-seed by editing OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD
  in ~/.config/containers/systemd/omnigent-server.container before starting.

  For tailnet access with a real (browser-trusted) HTTPS cert, add:
      podman exec woow-tailscale-gateway \\
          tailscale serve --bg --https=9444 http://127.0.0.1:8000
  then open https://woow-openclaw-services-1.tailb7a69b.ts.net:9444/

EOF
