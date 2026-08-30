#!/usr/bin/env bash
# Take the omnigent stack down. omnigent-postgres-data and omnigent-server-data
# are KEPT by default (they hold accounts + policies + artifacts); pass
# --purge to delete them too. pi-agent-data is EXTERNAL — never touched.
#
#   ./scripts/uninstall.sh            keep data volumes
#   ./scripts/uninstall.sh --purge    also delete omnigent-{postgres,server}-data
set -euo pipefail

QUADLET_DIR="${HOME}/.config/containers/systemd"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
PURGE="${1:-}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Stopping services"
systemctl --user disable --now omnigent-server-health.timer 2>/dev/null || true
systemctl --user stop omnigent-runner.service 2>/dev/null || true
systemctl --user stop omnigent-server.service 2>/dev/null || true
systemctl --user stop omnigent-postgres.service 2>/dev/null || true

say "Removing units"
for unit in omnigent-runner.container omnigent-server.container omnigent-postgres.container omnigent.network; do
    rm -f "${QUADLET_DIR}/${unit}"
done
rm -f "${USER_UNIT_DIR}/omnigent-server-health.service" \
      "${USER_UNIT_DIR}/omnigent-server-health.timer"
systemctl --user daemon-reload
for c in omnigent-runner omnigent-server omnigent-postgres; do
    podman rm -f "$c" 2>/dev/null || true
done

if [ "${PURGE}" = "--purge" ]; then
    say "Deleting data volumes (--purge)"
    for v in omnigent-server-data omnigent-postgres-data; do
        podman volume rm -f "$v" 2>/dev/null || true
    done
fi

say "Done. pi-agent-data volume was NOT touched (owned by sibling package)."
