#!/usr/bin/env bash
# scripts/upgrade.sh: move omnigent to the versions this checkout pins, with an automatic
# rollback of the units when the new version fails its smoke test; leaves writers stopped.
#
#   git pull && scripts/upgrade.sh [--no-backup]
#
#   1. snapshot the installed unit files and the install manifest
#   2. scripts/backup.sh --stop --include-secrets: consistent dump + volumes        (skipped with --no-backup)
#   3. scripts/install.sh: pulls the pinned server image, builds the runner tag if missing,
#      re-renders the units, restarts what changed, then runs tests/smoke.sh
#   4. on any failure: stop writers, put the snapshot back, print data restore command, exit 1
#
# The previous image tags are never deleted, so the restored units start the old versions.
# Upstream's database migrations are one-way: if the new server migrated the schema, the
# rollback needs the dump too, and this script prints the exact restore command for it.
# SMOKE_FORCE_FAIL=1 makes step 3 fail on purpose to exercise the rollback.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=omnigent
KEEP_SNAPSHOTS=5

backup=1
while (($#)); do
  case $1 in
    --no-backup) backup=0 ;;
    -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
python3 "$REPO/scripts/verify-server-image.py" || ql_die "server image receipt validation failed; nothing was changed"
ql_require_rootless
ql_require_user_systemd
ql_lock "$APP"
STATE=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
[[ -s $STATE/manifest ]] || ql_die "$APP is not installed (no $STATE/manifest); run scripts/install.sh"

# ---- 1. snapshot ------------------------------------------------------------------------------
snap=$STATE/rollback/$(date +%Y%m%d-%H%M%S)
(umask 077 && mkdir -p -- "$snap/files") || ql_die "cannot create $snap"
cp -p -- "$STATE/manifest" "$snap/manifest"
while read -r _ path; do
  [[ -f $path ]] || continue
  mkdir -p -- "$snap/files${path%/*}"
  cp -p -- "$path" "$snap/files$path"
done <"$STATE/manifest"
ql_info "snapshot of the installed units: $snap"
snaps=("$STATE"/rollback/*/) # timestamp names: glob order is age order
if ((${#snaps[@]} > KEEP_SNAPSHOTS)); then rm -rf -- "${snaps[@]:0:${#snaps[@]}-KEEP_SNAPSHOTS}"; fi

backup_dir=''
rollback() {
  local path rel
  systemctl --user stop omnigent-runner.service omnigent-server.service
  ql_warn "upgrade failed: rolling back to the snapshot $snap"
  if [[ -f $STATE/manifest ]]; then
    while read -r _ path; do
      grep -qF -- "  $path" "$snap/manifest" || rm -f -- "$path"
    done <"$STATE/manifest"
  fi
  while IFS= read -r -d '' rel; do
    rel=${rel#"$snap/files"}
    mkdir -p -- "${rel%/*}"
    cp -p -- "$snap/files$rel" "$rel"
  done < <(find "$snap/files" -type f -print0)
  cp -p -- "$snap/manifest" "$STATE/manifest"
  rm -f -- "$STATE/pending-restart"
  systemctl --user daemon-reload
  ql_warn "previous units restored; application services remain stopped because the database may have migrated"
  if [[ -n $backup_dir ]]; then
    ql_warn "restore the complete pre-upgrade checkpoint before starting the old application:"
    printf '    %q/scripts/restore.sh %q --with-secrets\n' "$REPO" "$backup_dir" >&2
  else
    ql_warn "--no-backup was used: supply a verified pre-upgrade backup before restarting"
  fi
  ql_warn "see docs/mcp-upgrade-rollback.md; units alone do not roll back Alembic"
  exit 1
}

# ---- 2. backup -------------------------------------------------------------------------------
if ((backup)); then
  backup_dir=$("$REPO/scripts/backup.sh" --stop --include-secrets) || ql_die "backup failed; nothing was changed"
  ql_info "pre-upgrade backup: $backup_dir"
fi

# ---- 3. install + smoke (install.sh runs tests/smoke.sh) ------------------------------------
if ! "$REPO/scripts/install.sh"; then rollback; fi
ql_info "upgrade to $(<"$REPO/VERSION") complete; rollback snapshot kept in $snap"
