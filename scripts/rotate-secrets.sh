#!/usr/bin/env bash
# scripts/rotate-secrets.sh: rotate the credentials of a running omnigent deployment.
#
#   scripts/rotate-secrets.sh --db | --admin | --runner | --all [--yes]
#
#   --db      generate a new database password, apply it with ALTER ROLE over the container's
#             unix socket (the SQL goes in on stdin, never in argv), replace the
#             omnigent-postgres-password and omnigent-database-url secrets, restart the server
#   --admin   change the admin password through /auth/users/me/password, replace the secret,
#             restart the server so its INIT_ADMIN environment stays in sync
#   --runner  change the daily operator password the same way, replace its secret and restart
#             the runner
#   --all     --db, then --admin, then --runner (two input lines in non-interactive mode)
#
# The values are never printed, never passed in argv and never written to disk. Run it right
# after adopting a deployment whose credentials were published (see README "Rotate the
# credentials that were committed").
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=omnigent
ENV_FILE=$HOME/.config/$APP/$APP.env

do_db=0 do_admin=0 do_runner=0 yes=0
while (($#)); do
  case $1 in
    --db) do_db=1 ;;
    --admin) do_admin=1 ;;
    --runner) do_runner=1 ;;
    --all) do_db=1 do_admin=1 do_runner=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,18p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
((do_db || do_admin || do_runner)) || ql_die "nothing to do: pass --db, --admin, --runner or --all (see --help)"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
command -v jq >/dev/null 2>&1 || ql_die "jq not found (sudo apt-get install jq)"
[[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found; run scripts/install.sh first"
ql_lock "$APP"
ql_env_load "$ENV_FILE"
BASE="http://$(ql_env_get OMNIGENT_BIND):$(ql_env_get OMNIGENT_PORT)"
USER_NAME=$(ql_env_get OMNIGENT_ADMIN_USERNAME)
RUNNER_USER=$(ql_env_get OMNIGENT_RUNNER_USERNAME)

# [A-Za-z0-9] only: the password also goes into DATABASE_URL, where anything else would
# have to be percent-encoded.
random_alnum() {
  local want=$1 out='' chunk
  while ((${#out} < want)); do
    chunk=$(head -c 768 /dev/urandom | base64 -w0) || return 1
    out+=${chunk//[!A-Za-z0-9]/}
  done
  printf '%s' "${out:0:want}"
}

if ((do_db)); then
  [[ $(podman inspect --format '{{.State.Status}}' omnigent-postgres 2>/dev/null || true) == running ]] \
    || ql_die "omnigent-postgres is not running; start the stack first"
  if ((!yes)) && [[ -t 0 ]]; then
    read -r -p "Generate a new database password and restart omnigent-server? [y/N] " a
    [[ $a == [yY] ]] || ql_die "aborted; nothing was changed"
  fi
  NEW_DB_PASSWORD=$(random_alnum 32) || ql_die "cannot read /dev/urandom"
  printf "ALTER ROLE omnigent PASSWORD '%s';\n" "$NEW_DB_PASSWORD" \
    | podman exec -i omnigent-postgres psql -v ON_ERROR_STOP=1 -U omnigent -d postgres -q \
    || ql_die "ALTER ROLE failed; nothing was changed"
  ql_secret_ensure omnigent-postgres-password env:NEW_DB_PASSWORD --replace
  # shellcheck disable=SC2034 # read by ql_secret_ensure through env:OMNIGENT_DATABASE_URL_VALUE
  OMNIGENT_DATABASE_URL_VALUE="postgresql+psycopg://omnigent:$NEW_DB_PASSWORD@omnigent-postgres:5432/omnigent"
  NEW_DB_PASSWORD=''
  ql_secret_ensure omnigent-database-url env:OMNIGENT_DATABASE_URL_VALUE --replace
  # shellcheck disable=SC2034 # cleared on purpose; ql_secret_ensure already read it
  OMNIGENT_DATABASE_URL_VALUE=''
  ql_info "restarting omnigent-server with the new database URL"
  systemctl --user restart omnigent-server.service || ql_die "restart failed; see journalctl --user -u omnigent-server -n 100"
  QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-server 180 \
    || ql_die "omnigent-server is not healthy after the database rotation; see journalctl --user -u omnigent-server -n 100"
  ql_info "database password rotated"
fi

read_new_password() { # read_new_password <account> <variable name>
  local account=$1 var=$2 value again
  if [[ -t 0 ]]; then
    read -r -s -p "New password for $account (not echoed): " value; echo >&2
    read -r -s -p "Repeat it: " again; echo >&2
    [[ $value == "$again" ]] || ql_die "the two passwords differ; nothing was changed"
  else
    IFS= read -r value || ql_die "no password for $account on stdin"
  fi
  [[ ${#value} -ge 8 ]] || ql_die 'password must be at least eight characters'
  printf -v "$var" '%s' "$value"
}

if ((do_admin)); then
  read_new_password "$USER_NAME" NEW_ADMIN_PASSWORD
  printf '%s\n' "$NEW_ADMIN_PASSWORD" \
    | BASE=$BASE USER_NAME=$USER_NAME SECRET_NAME=omnigent-admin-password \
      "$REPO/scripts/change-account-password.sh" || ql_die 'admin password change failed; the secret was not replaced'
  ql_secret_ensure omnigent-admin-password env:NEW_ADMIN_PASSWORD --replace \
    || ql_die 'account password changed but podman secret replacement failed: recover from a fresh login'
  NEW_ADMIN_PASSWORD=''
  systemctl --user restart omnigent-server.service || ql_die 'admin secret rotated but server restart failed'
  ql_info 'admin password rotated'
fi

if ((do_runner)); then
  read_new_password "$RUNNER_USER" NEW_RUNNER_PASSWORD
  printf '%s\n' "$NEW_RUNNER_PASSWORD" \
    | BASE=$BASE USER_NAME=$RUNNER_USER SECRET_NAME=omnigent-runner-password \
      "$REPO/scripts/change-account-password.sh" || ql_die 'operator password change failed; the secret was not replaced'
  ql_secret_ensure omnigent-runner-password env:NEW_RUNNER_PASSWORD --replace \
    || ql_die 'account password changed but podman secret replacement failed: recover from a fresh login'
  NEW_RUNNER_PASSWORD=''
  systemctl --user restart omnigent-runner.service || ql_die 'operator secret rotated but runner restart failed'
  ql_info 'operator password rotated'
fi

"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the rotation"
