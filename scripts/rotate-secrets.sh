#!/usr/bin/env bash
# scripts/rotate-secrets.sh: rotate the credentials of a running omnigent deployment.
#
#   scripts/rotate-secrets.sh --db | --admin | --all [--yes]
#
#   --db      generate a new database password, apply it with ALTER ROLE over the container's
#             unix socket (the SQL goes in on stdin, never in argv), replace the
#             omnigent-postgres-password and omnigent-database-url secrets, restart the server
#   --admin   read the NEW admin password on stdin, verify it with a login, replace the
#             omnigent-admin-password secret and restart the runner. Change the password in
#             the web UI first (Settings -> Account): v0.12.0 has no API for it, and the
#             runner needs the secret to match whatever the account now uses.
#   --all     --db, then --admin
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

do_db=0 do_admin=0 yes=0
while (($#)); do
  case $1 in
    --db) do_db=1 ;;
    --admin) do_admin=1 ;;
    --all) do_db=1 do_admin=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,18p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
((do_db || do_admin)) || ql_die "nothing to do: pass --db, --admin or --all (see --help)"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
command -v jq >/dev/null 2>&1 || ql_die "jq not found (sudo apt-get install jq)"
[[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found; run scripts/install.sh first"
ql_lock "$APP"
ql_env_load "$ENV_FILE"
BASE="http://$(ql_env_get OMNIGENT_BIND):$(ql_env_get OMNIGENT_PORT)"
USER_NAME=$(ql_env_get OMNIGENT_ADMIN_USERNAME)

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

if ((do_admin)); then
  ql_info "change the password of the admin account '$USER_NAME' in the web UI first ($BASE, Settings -> Account)"
  if [[ -t 0 ]]; then
    read -r -s -p "New admin password (not echoed): " NEW_ADMIN_PASSWORD
    echo >&2
    read -r -s -p "Repeat it: " again
    echo >&2
    [[ $NEW_ADMIN_PASSWORD == "$again" ]] || ql_die "the two passwords differ; nothing was changed"
    again=''
  else
    IFS= read -r NEW_ADMIN_PASSWORD || ql_die "no password on stdin"
  fi
  [[ -n $NEW_ADMIN_PASSWORD ]] || ql_die "the password is empty"
  # Verify it against the running server before storing it, so the runner cannot end up
  # with a secret that does not log in. The value goes to jq on stdin, not in argv.
  token_len=$(jq -Rn --arg u "$USER_NAME" '{username: $u, password: input}' <<<"$NEW_ADMIN_PASSWORD" \
    | curl -s -m 10 -H 'Content-Type: application/json' --data @- "$BASE/auth/login" 2>/dev/null \
    | jq -r '(.token // "") | length' 2>/dev/null) || token_len=0
  ((token_len > 0)) || ql_die "that password does not log in as '$USER_NAME' at $BASE; change it in the web UI first, then re-run"
  ql_secret_ensure omnigent-admin-password env:NEW_ADMIN_PASSWORD --replace
  NEW_ADMIN_PASSWORD=''
  ql_info "restarting omnigent-runner so it picks up the new admin password"
  systemctl --user restart omnigent-runner.service || ql_die "restart failed; see journalctl --user -u omnigent-runner -n 100"
  ql_info "admin password secret rotated"
fi

"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed after the rotation"
