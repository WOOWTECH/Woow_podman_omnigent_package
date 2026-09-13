#!/usr/bin/env bash
# scripts/uninstall.sh: remove the omnigent Quadlet units. Keeps data by default.
#
#   scripts/uninstall.sh                   stop + remove the units; keep the volumes, the
#                                          network, the secrets, the images and the env file
#                                          (a re-install adopts them unchanged)
#   scripts/uninstall.sh --purge [--yes]   also delete omnigent-{postgres,server,pi}-data,
#                                          the network and the secrets, after a final backup
#                                          to ~/backups/omnigent/. The ONLY way this repo
#                                          deletes data.
#   scripts/uninstall.sh --purge-images    also remove the localhost/woow-omnigent-runner:* images
#   scripts/uninstall.sh --dry-run         report what would be removed
#
# pi-agent-data (Woow_podman_pi_agent_package, shared mode) is never deleted: this repo does
# not install that volume unit, so --purge cannot reach it. The env file in ~/.config/omnigent/
# is kept; delete it yourself.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=omnigent
BACKUP_DIR=$HOME/backups/$APP
# exported before --purge deletes them (never pi-agent-data)
DATA_VOLUMES=(omnigent-postgres-data omnigent-server-data omnigent-pi-data)
IMAGE_REPO=localhost/woow-omnigent-runner
# ------------------------------------------------------------------------------------------

purge=0 yes=0 purge_images=0
while (($#)); do
  case $1 in
    --purge) purge=1 ;;
    --purge-images) purge_images=1 ;;
    --yes) yes=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}
ql_require_rootless
ql_lock "$APP"

if ((purge)); then
  if ((!yes)) && [[ $DRY != 1 ]]; then
    [[ -t 0 ]] || ql_die "--purge deletes the database, the server volume and the secrets; add --yes to confirm non-interactively"
    read -r -p "Type '$APP' to delete its volumes (accounts, artifacts, private pi state), network and secrets: " answer
    [[ $answer == "$APP" ]] || ql_die "aborted; nothing was deleted"
  fi
  if [[ $DRY != 1 ]]; then
    # A logical dump needs a running database, so back up before ql_uninstall_units stops it.
    # --stop: the volumes are deleted a few lines below, so there is nothing to keep running
    # and the export is consistent instead of podman's "may be inconsistent".
    if [[ $(podman inspect --format '{{.State.Status}}' omnigent-postgres 2>/dev/null || true) == running ]]; then
      "$REPO/scripts/backup.sh" --stop --dest "$BACKUP_DIR" >/dev/null || ql_die "the final backup failed; nothing was deleted"
    else
      ql_warn "omnigent-postgres is not running: exporting the volumes instead of a database dump"
      for v in "${DATA_VOLUMES[@]}"; do
        if podman volume exists "$v"; then ql_backup_volume "$v" "$BACKUP_DIR" >/dev/null; fi
      done
    fi
  fi
  ql_uninstall_units "$APP" --purge
  # A host that switched to shared pi state has no omnigent-pi.volume in its manifest any
  # more, so --purge above cannot see the private volume it left behind.
  if [[ $DRY != 1 ]] && podman volume exists omnigent-pi-data 2>/dev/null; then
    if podman volume rm omnigent-pi-data >/dev/null 2>&1; then
      ql_info "removed volume omnigent-pi-data"
    else
      ql_warn "could not remove volume omnigent-pi-data (in use?)"
    fi
  fi
else
  ql_uninstall_units "$APP"
fi

if ((purge_images)); then
  mapfile -t imgs < <(podman images --format '{{.Repository}}:{{.Tag}}' | grep -E "^${IMAGE_REPO}:" || true)
  for img in "${imgs[@]}"; do
    if [[ $DRY == 1 ]]; then ql_info "[dry-run] would remove image $img"; continue; fi
    if podman rmi "$img" >/dev/null 2>&1; then ql_info "removed image $img"; else ql_warn "could not remove image $img (in use?)"; fi
  done
fi
