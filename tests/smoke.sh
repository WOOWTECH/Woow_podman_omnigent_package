#!/usr/bin/env bash
# tests/smoke.sh: post-install checks of a running omnigent stack. Read-only apart from
# `podman healthcheck run` and one admin login.
#
#   tests/smoke.sh            exit 0 only when every check passes
#   SMOKE_FORCE_FAIL=1 ...    fail on purpose (exercises scripts/upgrade.sh's rollback)
#
# Reads bind/port/username/pi mode from ~/.config/omnigent/omnigent.env. The admin password
# is piped from the podman secret into curl: never printed, never in argv. The extra checks
# in tests/smoke-{container,pi-integration,runner-dialin}.sh go deeper into the runner.
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
export QL_LOG_PREFIX=smoke
APP=omnigent
ENV_FILE=$HOME/.config/$APP/$APP.env

pass=0 fail=0
ok() { printf '  PASS  %s\n' "$*"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
check() { # check <description> <command...>
  local d=$1
  shift
  if "$@"; then ok "$d"; else bad "$d"; fi
}

[[ -f $ENV_FILE ]] || { echo "smoke: $ENV_FILE not found (not installed?)" >&2; exit 1; }
ql_env_load "$ENV_FILE"
BIND=$(ql_env_get OMNIGENT_BIND)
PORT=$(ql_env_get OMNIGENT_PORT)
USER_NAME=$(ql_env_get OMNIGENT_ADMIN_USERNAME)
PI_STATE=$(ql_env_get OMNIGENT_PI_STATE private)
BASE=http://$BIND:$PORT

healthy() { # healthy <container>: podman's status, refreshed by one active check if needed
  [[ $(podman inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null) == healthy ]] && return 0
  podman healthcheck run "$1" >/dev/null 2>&1
}
runner_log_has() { podman logs omnigent-runner 2>&1 | grep -qF -- "$1"; }
json_ok() { curl -s -m 10 "$BASE$1" 2>/dev/null | jq -e "$2" >/dev/null 2>&1; } # json_ok <path> <jq filter>

echo "== $APP at $BASE (pi state: $PI_STATE)"
for u in omnigent-postgres.service omnigent-server.service omnigent-runner.service omnigent-server-health.timer; do
  check "unit $u is active" systemctl --user is-active --quiet "$u"
done
check "omnigent-postgres is healthy" healthy omnigent-postgres
check "omnigent-server is healthy" healthy omnigent-server
check 'GET /health -> {"status":"ok"}' json_ok /health '.status == "ok"'

listeners=$(ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')
check "port $PORT listens only on $BIND:$PORT (got: ${listeners:-none})" test "${listeners% }" = "$BIND:$PORT"
check "GET /v1/info -> needs_setup=false (the admin is claimed)" json_ok /v1/info '.needs_setup == false'

# admin login with the secret; only the presence of a token is checked, nothing is printed
login_ok() {
  podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password 2>/dev/null \
    | jq -Rn --arg u "$USER_NAME" '{username: $u, password: input}' \
    | curl -s -m 10 -H 'Content-Type: application/json' --data @- "$BASE/auth/login" 2>/dev/null \
    | jq -e '(.token // "") | length > 0' >/dev/null 2>&1
}
check "POST /auth/login as '$USER_NAME' with the omnigent-admin-password secret -> token" login_ok

# The runner logs in and registers a host on every start; give a fresh start a moment.
QL_POLL_INTERVAL=5 ql_wait_until 180 "the runner to log in and start its host daemon" \
  runner_log_has 'tailing' >/dev/null 2>&1
check "runner logged in (auth_tokens.json)" runner_log_has 'auth_tokens.json'
check "runner host daemon is up (log tail started)" runner_log_has 'tailing'

want_vol=omnigent-pi-data
[[ $PI_STATE == private ]] || want_vol=pi-agent-data
mounts=$(podman inspect --format '{{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}' omnigent-runner 2>/dev/null || true)
check "runner mounts $want_vol at /data/pi-agent" grep -qw -- "$want_vol:/data/pi-agent" <<<"$mounts"

pg_env=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-postgres 2>/dev/null | cut -d= -f1)
pg_env_ok() { grep -qx POSTGRES_PASSWORD_FILE <<<"$pg_env" && ! grep -qx POSTGRES_PASSWORD <<<"$pg_env"; }
check "postgres gets POSTGRES_PASSWORD_FILE and no POSTGRES_PASSWORD" pg_env_ok
units_text=$(systemctl --user cat omnigent-postgres omnigent-server omnigent-runner 2>/dev/null || true)
check "no plaintext password or credential URL in the units" \
  test "$(grep -ciE '(PASSWORD|DATABASE_URL)=[^[:space:]]|://[^:/@[:space:]]+:[^@[:space:]]+@' <<<"$units_text")" = 0

if [[ ${SMOKE_FORCE_FAIL:-0} == 1 ]]; then bad "SMOKE_FORCE_FAIL=1 (forced failure)"; fi
echo "== $pass passed, $fail failed"
((fail == 0))
