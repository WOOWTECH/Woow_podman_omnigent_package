#!/usr/bin/env bash
# scripts/restore.sh: restore a scripts/backup.sh directory into the installed units.
#
#   scripts/restore.sh <backup_dir> [--with-secrets] [--yes]
#
#   --with-secrets   also restore the database and admin passwords from secrets.env: the
#                    role password is applied with ALTER ROLE and both DB secrets are
#                    replaced, and the admin secret is set to the one that matches the
#                    password hash in the dump
#   --yes            do not ask for confirmation
#
# Stops the runner and the server, DROPS and recreates the omnigent database from the dump,
# replaces omnigent-server-data (and omnigent-pi-data when the backup has it), starts
# everything again and runs tests/smoke.sh. Install the units first (scripts/install.sh).
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
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

shopt -s nullglob
dumps=("$dir"/"$APP"-*.dump)
((${#dumps[@]} == 1)) || ql_die "expected exactly one $APP-*.dump in $dir, found ${#dumps[@]}"
dump=${dumps[0]}
if [[ -f $dump.sha256 ]]; then
  (cd -- "$dir" && sha256sum -c --quiet -- "${dump##*/}.sha256") || ql_die "checksum mismatch for $dump"
else
  ql_warn "no $dump.sha256; restoring without a checksum"
fi
[[ $(systemctl --user show -p LoadState --value omnigent-server.service 2>/dev/null) == loaded ]] \
  || ql_die "the omnigent units are not installed; run scripts/install.sh first"
((with_secrets == 0)) || [[ -f $dir/secrets.env ]] || ql_die "--with-secrets: $dir/secrets.env not found"

if ((!yes)); then
  [[ -t 0 ]] || ql_die "restore drops the current omnigent database; add --yes to confirm non-interactively"
  read -r -p "Drop the omnigent database and restore ${dump##*/}? Type '$APP' to continue: " answer
  [[ $answer == "$APP" ]] || ql_die "aborted; nothing was changed"
fi

ql_info "stopping the runner and the server (the database stays up for the restore)"
systemctl --user stop omnigent-runner.service omnigent-server.service
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
for t in "$dir"/omnigent-pi-data-*.tar; do
  if [[ -f $HOME/.config/containers/systemd/omnigent-pi.volume ]]; then
    restore_volume omnigent-pi-data omnigent-pi-volume.service "$t"
  else
    ql_warn "this host runs shared pi state; skipping ${t##*/}"
  fi
done

if ((with_secrets)); then
  ql_env_load "$dir/secrets.env"
  if [[ -n ${QL_ENV[OMNIGENT_POSTGRES_PASSWORD]+x} ]]; then
    # The dump carries no role password: put the backup's password back on the role, then
    # replace both database secrets so the server can connect again.
    pw=${QL_ENV[OMNIGENT_POSTGRES_PASSWORD]}
    [[ $pw =~ ^[A-Za-z0-9._~-]+$ ]] || ql_die "the backup's database password needs URL encoding; rotate it instead (scripts/rotate-secrets.sh --db)"
    printf "ALTER ROLE omnigent PASSWORD '%s';\n" "$pw" | psql_stdin || ql_die "ALTER ROLE failed"
    ql_secret_ensure omnigent-postgres-password env:OMNIGENT_POSTGRES_PASSWORD --replace
    # shellcheck disable=SC2034 # read by ql_secret_ensure through env:OMNIGENT_DATABASE_URL_VALUE
    OMNIGENT_DATABASE_URL_VALUE="postgresql+psycopg://omnigent:$pw@omnigent-postgres:5432/omnigent"
    pw=''
    ql_secret_ensure omnigent-database-url env:OMNIGENT_DATABASE_URL_VALUE --replace
    # shellcheck disable=SC2034 # cleared on purpose; ql_secret_ensure already read it
    OMNIGENT_DATABASE_URL_VALUE=''
  fi
  if [[ -n ${QL_ENV[OMNIGENT_ADMIN_PASSWORD]+x} ]]; then
    ql_secret_ensure omnigent-admin-password env:OMNIGENT_ADMIN_PASSWORD --replace
  fi
fi

systemctl --user restart omnigent-server.service omnigent-runner.service
QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-server 180 || ql_die "omnigent-server did not become healthy after the restore"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the restore"
ql_info "restore complete"
