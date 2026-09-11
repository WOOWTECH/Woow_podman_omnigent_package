#!/usr/bin/env bash
# scripts/backup.sh: back up omnigent into a new directory.
#
#   scripts/backup.sh [--dest DIR] [--include-secrets]
#
#   --dest DIR          parent directory (default ~/backups/omnigent); a <timestamp>/
#                       subdirectory is created in it and printed on stdout
#   --include-secrets   also write the podman secrets to secrets.env (0600). Needed to
#                       restore onto another host: the database dump does not carry the
#                       role password, and the admin password hash in it only matches the
#                       admin secret of the same moment.
#
# Contents: omnigent-<ts>.dump (pg_dump -Fc: accounts, policies, sessions), an export of
# omnigent-server-data, an export of omnigent-pi-data in private mode, and a copy of the env
# file. Everything is 0600 in a 0700 directory. Restore with scripts/restore.sh <dir>.
# pi-agent-data (shared mode) belongs to Woow_podman_pi_agent_package and is not backed up here.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=omnigent
ENV_FILE=$HOME/.config/$APP/$APP.env
SECRET_VARS=(omnigent-postgres-password:OMNIGENT_POSTGRES_PASSWORD omnigent-admin-password:OMNIGENT_ADMIN_PASSWORD)

dest=$HOME/backups/$APP include_secrets=0
while (($#)); do
  case $1 in
    --dest) dest=${2:?--dest needs a directory}; shift ;;
    --include-secrets) include_secrets=1 ;;
    -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_require_rootless
[[ $(podman inspect --format '{{.State.Status}}' omnigent-postgres 2>/dev/null || true) == running ]] \
  || ql_die "omnigent-postgres is not running; start it (systemctl --user start omnigent-postgres) so pg_dump can run"

base=$dest/$(date +%Y%m%d-%H%M%S) out=$dest/$(date +%Y%m%d-%H%M%S) n=2
while [[ -e $out ]]; do out=$base-$n; n=$((n + 1)); done
(umask 077 && mkdir -p -- "$out") || ql_die "cannot create $out"
chmod 700 "$out"

dump=$out/$APP-${out##*/}.dump
if ! (umask 077 && podman exec omnigent-postgres pg_dump -U omnigent -d omnigent -Fc >"$dump.partial"); then
  rm -f -- "$dump.partial"
  ql_die "pg_dump failed; nothing was written"
fi
mv -f -- "$dump.partial" "$dump"
(cd -- "$out" && umask 077 && sha256sum -- "${dump##*/}" >"${dump##*/}.sha256")
ql_info "dumped the database -> $dump ($(du -h -- "$dump" | cut -f1))"

ql_backup_volume omnigent-server-data "$out" >/dev/null
if podman volume exists omnigent-pi-data 2>/dev/null; then ql_backup_volume omnigent-pi-data "$out" >/dev/null; fi
if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$out/${ENV_FILE##*/}"; fi

if ((include_secrets)); then
  (
    umask 077
    for sv in "${SECRET_VARS[@]}"; do
      name=${sv%%:*} var=${sv#*:}
      if value=$(podman secret inspect --showsecret --format '{{.SecretData}}' "$name" 2>/dev/null); then
        printf '%s=%s\n' "$var" "$value"
      else
        ql_warn "secret $name not found; not in secrets.env"
      fi
    done >"$out/secrets.env"
  )
  ql_warn "$out/secrets.env holds the database and admin passwords in plain text (0600): keep the backup private"
fi
ql_info "backup complete: $out"
printf '%s\n' "$out"
