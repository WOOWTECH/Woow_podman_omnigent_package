#!/usr/bin/env bash
# scripts/restore.sh: restore a scripts/backup.sh directory into the installed units.
#
#   scripts/restore.sh <backup_dir> [--with-secrets] [--yes]
#
#   --with-secrets   also restore the database, admin and operator passwords from secrets.json (or a legacy secrets.env): the
#                    role password is applied with ALTER ROLE and both DB secrets are
#                    replaced, and the admin secret is set to the one that matches the
#                    password hash in the dump
#   --yes            do not ask for confirmation
#
# Stops the runner and the server, DROPS and recreates the omnigent database from the dump,
# replaces omnigent-server-data (and omnigent-pi-data when the backup has it), starts
# everything again and runs tests/smoke.sh after starting the health timer. Requires the
# matching checkpoint units/env already installed, including old image pins and Pi mode.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=writer-state.sh
. "$REPO/scripts/writer-state.sh"
APP=omnigent

dir='' with_secrets=0 yes=0
while (($#)); do
  case $1 in
    --with-secrets) with_secrets=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    -*) ql_die "unknown option $1 (see --help)" ;;
    *) [[ -z $dir ]] || ql_die "one backup directory only"; dir=$1 ;;
  esac
  shift
done
[[ -n $dir && -d $dir ]] || ql_die "usage: scripts/restore.sh <backup_dir> [--with-secrets] [--yes]"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
ql_lock "$APP"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/omnigent-restore.XXXXXX")
chmod 700 "$WORK"
ql_cleanup restore_work rm -rf "$WORK"

shopt -s nullglob
dumps=("$dir"/"$APP"-*.dump)
((${#dumps[@]} == 1)) || ql_die "expected exactly one $APP-*.dump in $dir, found ${#dumps[@]}"
dump=${dumps[0]}
[[ -f $dump.sha256 ]] || ql_die "missing checksum for ${dump##*/}"
(cd -- "$dir" && sha256sum -c --quiet -- "${dump##*/}.sha256") || ql_die "checksum mismatch for $dump"
for archive in "$dir"/omnigent-*-data-*.tar; do
  [[ -f $archive.sha256 ]] || ql_die "missing checksum for ${archive##*/}"
  (cd -- "$dir" && sha256sum -c --quiet -- "${archive##*/}.sha256") \
    || ql_die "checksum mismatch for ${archive##*/}"
  python3 "$REPO/scripts/backup-checkpoint.py" check-archive --archive "$archive" \
    || ql_die "invalid volume archive; no data changed"
done
server_archives=("$dir"/omnigent-server-data-*.tar)
((${#server_archives[@]} == 1)) || ql_die "expected exactly one server data archive (artifacts/config/cookie secret)"
[[ $(systemctl --user show -p LoadState --value omnigent-server.service 2>/dev/null) == loaded ]] \
  || ql_die "the omnigent units are not installed; restore the matching checkpoint units first (see docs/mcp-upgrade-rollback.md)"
pi_state=$(python3 "$REPO/scripts/backup-checkpoint.py" check --backup "$dir" \
  --quadlet-dir "$(_ql_quadlet_dir)" --systemd-dir "$(_ql_sd_user_dir)" --env "$HOME/.config/omnigent/omnigent.env") \
  || ql_die "restore requires matching checkpoint units/config, including old image pins and Pi mode; no data changed"
pi_archives=("$dir"/omnigent-pi-data-*.tar)
if [[ $pi_state == private ]]; then
  ((${#pi_archives[@]} == 1)) || ql_die "private checkpoint requires exactly one private Pi archive"
else
  ((${#pi_archives[@]} == 0)) || ql_die "shared checkpoint must not contain a private Pi archive"
fi
if ((with_secrets)); then
  python3 "$REPO/scripts/backup-checkpoint.py" prepare-secrets --backup "$dir" --output "$WORK" \
    || ql_die "invalid or incomplete secrets archive; no data changed"
fi
podman exec -i omnigent-postgres pg_restore --list <"$dump" >/dev/null \
  || ql_die "invalid PostgreSQL dump or unavailable database container; no data changed"

if ((!yes)); then
  [[ -t 0 ]] || ql_die "restore drops the current omnigent database; add --yes to confirm non-interactively"
  read -r -p "Drop the omnigent database and restore ${dump##*/}? Type '$APP' to continue: " answer
  [[ $answer == "$APP" ]] || ql_die "aborted; nothing was changed"
fi

ql_info "stopping the runner and the server (the database stays up for the restore)"
# Reload the verified old unit files before starting any checkpoint services.
omnigent_stop_writers
systemctl --user daemon-reload
systemctl --user start omnigent-postgres.service
QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-postgres 180 || ql_die "omnigent-postgres is not healthy"

restore_volume() { # restore_volume <volume> <volume-unit> <tar>
  podman volume exists "$1" && { podman volume rm "$1" >/dev/null || ql_die "cannot remove volume $1 (still in use?)"; }
  systemctl --user restart "$2" || ql_die "cannot recreate $1 through $2"
  podman volume import "$1" "$3" || ql_die "podman volume import into $1 failed"
  ql_info "restored $1 from ${3##*/}"
}

psql_stdin() { podman exec -i omnigent-postgres psql -v ON_ERROR_STOP=1 -U omnigent -d postgres -q; }
printf 'DROP DATABASE IF EXISTS omnigent;\nCREATE DATABASE omnigent OWNER omnigent;\n' | psql_stdin \
  || ql_die "could not drop and recreate the omnigent database"
podman exec -i omnigent-postgres pg_restore -U omnigent -d omnigent --no-owner <"$dump" \
  || ql_die "pg_restore failed; the database is empty now, re-run restore"
ql_info "restored the database from ${dump##*/}"

for t in "$dir"/omnigent-server-data-*.tar; do restore_volume omnigent-server-data omnigent-server-volume.service "$t"; done
if [[ $pi_state == private ]]; then
  restore_volume omnigent-pi-data omnigent-pi-volume.service "${pi_archives[0]}"
fi

if ((with_secrets)); then
  psql_stdin <"$WORK/restore-role.sql" || ql_die "ALTER ROLE failed"
  for name in omnigent-postgres-password omnigent-database-url omnigent-admin-password omnigent-runner-password; do
    ql_secret_ensure "$name" "file:$WORK/$name" --replace
  done
fi

# The preflight proved installed units AND env match this checkpoint byte for byte.
# Never re-render using the candidate checkout: the recorded old image pins must stay.
systemctl --user restart omnigent-server.service omnigent-runner.service
QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-server 180 || ql_die "omnigent-server did not become healthy after the restore"
systemctl --user start omnigent-server-health.timer || ql_die "could not start health timer"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the restore"
ql_info "restore complete"
