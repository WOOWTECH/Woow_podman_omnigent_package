#!/usr/bin/env bash
# scripts/install.sh: install or update the omnigent stack (postgres + server + runner) as
# rootless Quadlet units (podman >= 4.9, systemd --user, linger). Idempotent: an unchanged
# re-run restarts nothing.
#
#   scripts/install.sh [--port N] [--bind ADDR] [--pi-state private|shared] [--set KEY=VALUE]...
#                      [--rebuild | --no-build] [--no-start] [--dry-run] [--yes]
#
#   --port N            web UI port (OMNIGENT_PORT, default 8000); saved in the env file
#   --bind ADDR         publish address (OMNIGENT_BIND, default 127.0.0.1); saved in the env file
#   --pi-state MODE     private (own volume, default) or shared (pi-web's pi-agent-data)
#   --set KEY=VALUE     set any key of config/omnigent.env.example in the env file
#   --rebuild           rebuild the runner image even when the VERSION tag already exists
#   --no-build          never build; the runner image tag must already exist
#   --no-start          install the files and daemon-reload only
#   --dry-run           render, validate and report what would change; change nothing
#   --yes               accepted for symmetry with the other scripts (nothing to confirm)
#
# Per-host values live in ~/.config/omnigent/omnigent.env (0600), created from
# config/omnigent.env.example on the first run. Passwords are podman secrets.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=render-args.sh
. "$REPO/scripts/render-args.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=omnigent
ENV_FILE=$HOME/.config/$APP/$APP.env
EXAMPLE=$REPO/config/$APP.env.example
PODMAN_MIN=4.9
VERSION=$(<"$REPO/VERSION")
RUNNER_IMAGE=localhost/woow-omnigent-runner:$VERSION
# container name : unit that Quadlet generates for it (legacy-collision guard)
CONTAINERS=(omnigent-postgres:omnigent-postgres.service omnigent-server:omnigent-server.service omnigent-runner:omnigent-runner.service)
# units to start / restart (generated services and the plain health timer)
UNITS=(omnigent-postgres.service omnigent-server.service omnigent-runner.service omnigent-server-health.timer)
PG_VOLUME=omnigent-postgres-data
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
# ------------------------------------------------------------------------------------------

usage() { sed -n '2,21p' "$0"; }
sets=() build=auto no_start=0
while (($#)); do
  case $1 in
    --port) sets+=("OMNIGENT_PORT=${2:?--port needs a value}"); shift ;;
    --bind) sets+=("OMNIGENT_BIND=${2:?--bind needs a value}"); shift ;;
    --pi-state) sets+=("OMNIGENT_PI_STATE=${2:?--pi-state needs private or shared}"); shift ;;
    --pi-state=*) sets+=("OMNIGENT_PI_STATE=${1#--pi-state=}") ;;
    --set) sets+=("${2:?--set needs KEY=VALUE}"); shift ;;
    --rebuild) build=always ;;
    --no-build) build=never ;;
    --no-start) no_start=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    --yes) ;;
    -h | --help) usage; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}

# ---- 1. host preflight ---------------------------------------------------------------------
ql_preflight "$PODMAN_MIN"
for t in curl jq; do command -v "$t" >/dev/null 2>&1 || ql_die "$t not found (sudo apt-get install $t)"; done
ql_enable_linger
# The migration / converge wrapper in this repo already holds this app's lock and then calls
# install.sh; without this the nested ql_lock aborts the cutover half way through. Same
# convention as Woow_podman_nextcloud's install/backup/restore.
[[ ${WOOW_QL_LOCK_HELD:-} == "$APP" ]] || ql_lock "$APP"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# ---- 2. per-host settings (D2: rendered from the env file at install time) -----------------
ql_env_ensure "$EXAMPLE" "$ENV_FILE"
# Settings are staged in a private copy and saved only after every check below passed, so a
# rejected --port/--bind/--set never lands in the env file. A dry run never saves them.
envsrc=$WORK/$APP.env
if [[ -f $ENV_FILE ]]; then cp -- "$ENV_FILE" "$envsrc"; else cp -- "$EXAMPLE" "$envsrc"; QL_ENV_CREATED=1; fi
chmod 600 "$envsrc"
setenv() { QL_DRY_RUN=0 ql_env_set "$envsrc" "$1" "$2"; }
[[ $QL_ENV_CREATED != 1 ]] || ql_info "created $ENV_FILE with defaults; edit it and re-run to change them"
for kv in "${sets[@]}"; do
  [[ $kv == *=* ]] || ql_die "--set wants KEY=VALUE, got '$kv'"
  grep -q "^${kv%%=*}=" "$EXAMPLE" || ql_die "--set: ${kv%%=*} is not a setting of ${EXAMPLE##*/}"
  setenv "${kv%%=*}" "${kv#*=}"
done
ql_env_load "$envsrc"

BIND=$(ql_env_get OMNIGENT_BIND)
PORT=$(ql_env_get OMNIGENT_PORT)
USER_NAME=$(ql_env_get OMNIGENT_ADMIN_USERNAME)
PI_STATE=$(ql_env_get OMNIGENT_PI_STATE private)
ql_assert_match OMNIGENT_BIND "$BIND" '(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}'
[[ $BIND != 0.0.0.0 ]] || ql_die "OMNIGENT_BIND=0.0.0.0 would publish the web UI on every network; use 127.0.0.1 or one LAN IP"
ql_assert_match OMNIGENT_PORT "$PORT" '[1-9][0-9]{0,4}'
((PORT <= 65535)) || ql_die "OMNIGENT_PORT=$PORT is not a TCP port"
[[ $BIND == 127.0.0.1 ]] || ql_warn "OMNIGENT_BIND=$BIND: the web UI is reachable from that network, not only from this host"
# Rendered into Environment=: Quadlet splits on blanks and systemd would expand "$NAME".
ql_assert_match OMNIGENT_ACCOUNTS_BASE_URL "$(ql_env_get OMNIGENT_ACCOUNTS_BASE_URL)" '(https?://[^[:space:]"'"'"'\\$]+)?'
ql_assert_match OMNIGENT_ADMIN_USERNAME "$USER_NAME" '[A-Za-z0-9][A-Za-z0-9._@-]{0,63}'
ql_assert_match OMNIGENT_PI_STATE "$PI_STATE" 'private|shared'

# ---- 3. legacy guards ------------------------------------------------------------------------
for c in "${CONTAINERS[@]}"; do ql_check_container_collision "${c%%:*}" "${c#*:}"; done
# The port must be free, unless our running server is the one already publishing it.
published=$(sed -n 's/^PublishPort=//p' "$QDIR/omnigent-server.container" 2>/dev/null || true)
if [[ $published != "$BIND:$PORT:8000" || $(systemctl --user is-active omnigent-server.service 2>/dev/null || true) != active ]] \
  && command -v ss >/dev/null 2>&1 && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null || true) ]]; then
  ql_die "port $PORT is already in use on this host (ss -ltnp 'sport = :$PORT'); pick another with --port"
fi
# A database volume from a pre-conversion deployment keeps the password it was created with;
# only a secret holding that password can open it.
if podman volume exists "$PG_VOLUME" 2>/dev/null && ! podman secret exists omnigent-postgres-password 2>/dev/null; then
  ql_die "volume $PG_VOLUME exists but the podman secret omnigent-postgres-password does not: create the secret with the database's current password first (README \"Migrating an existing deployment\")"
fi
if [[ $PI_STATE == shared ]]; then
  [[ -f $QDIR/pi-agent-data.volume ]] \
    || ql_die "OMNIGENT_PI_STATE=shared needs pi-agent-data.volume from Woow_podman_pi_agent_package in $QDIR; install that package first, or use --pi-state private"
  ql_warn "shared pi state: the runner (pi $(sed -n 's/^ARG PI_CODING_AGENT_VERSION=//p' "$REPO/Containerfile.runner")) writes pi-web's pi-agent-data volume, whose on-disk format is not versioned; keep the pi versions in step"
fi

# ---- 4. render the units and validate them against the podman 4.9.3 generator -------------
mkdir -p "$WORK/src" "$WORK/out"
cp -p "$REPO"/quadlet/*.container "$REPO"/quadlet/omnigent.network \
  "$REPO"/quadlet/omnigent-postgres.volume "$REPO"/quadlet/omnigent-server.volume \
  "$REPO"/systemd/*.service "$REPO"/systemd/*.timer "$WORK/src/"
# private mode: this package's own pi volume; shared mode: the runner references pi_agent's
[[ $PI_STATE != private ]] || cp -p "$REPO/quadlet/omnigent-pi.volume" "$WORK/src/"
RENDER_ARGS=()
render_args "$envsrc"
ql_render "$WORK/src" "$envsrc" "$REPO/quadlet/render-vars" "$WORK/out" "${RENDER_ARGS[@]}"
ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" || ql_die "the rendered units failed the dry-run; nothing was installed"
for f in "$WORK/out"/*; do
  u=$(ql_unit_for "$f")
  [[ -z $u ]] || ql_check_unit_shadow "$u" "$APP"
done

# Every check passed: only now do new --port/--bind/--set values reach the env file.
if [[ $DRY != 1 ]] && ! cmp -s -- "$envsrc" "$ENV_FILE"; then
  install -m 600 -- "$envsrc" "$ENV_FILE" || ql_die "cannot update $ENV_FILE"
  ql_info "saved the new settings in $ENV_FILE"
fi

# ---- 5. images and secrets, before any unit changes -------------------------------------------
built=0
if [[ $build == always ]] || ! podman image exists "$RUNNER_IMAGE"; then
  [[ $build != never ]] || ql_die "image $RUNNER_IMAGE does not exist and --no-build was given"
  if [[ $DRY == 1 ]]; then
    ql_info "[dry-run] would build $RUNNER_IMAGE"
  else
    ql_info "building $RUNNER_IMAGE (podman build --format docker; about 5 minutes on a small host)"
    podman build --format docker -t "$RUNNER_IMAGE" --build-arg "BUILD_VERSION=$VERSION" \
      -f "$REPO/Containerfile.runner" "$REPO" || ql_die "podman build failed; nothing was changed"
    built=1
  fi
fi
# pulls the pinned server and postgres images, so a slow pull never runs inside a start timeout
if podman image exists "$RUNNER_IMAGE"; then ql_pull_images "$WORK/out"; fi

ql_secret_ensure omnigent-postgres-password random:32
ql_secret_ensure omnigent-admin-password random:24
db_url_changed=0
if podman secret exists omnigent-postgres-password 2>/dev/null; then
  # DATABASE_URL is derived from the password secret on every run, so the two never drift.
  pw=$(podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-postgres-password) \
    || ql_die "cannot read the podman secret omnigent-postgres-password"
  [[ $pw =~ ^[A-Za-z0-9._~-]+$ ]] \
    || ql_die "the omnigent-postgres-password secret has characters that need URL encoding; rotate it with scripts/rotate-secrets.sh --db"
  # shellcheck disable=SC2034 # read by ql_secret_ensure through env:OMNIGENT_DATABASE_URL_VALUE
  OMNIGENT_DATABASE_URL_VALUE="postgresql+psycopg://omnigent:$pw@omnigent-postgres:5432/omnigent"
  pw=''
  QL_SECRET_CHANGED=0
  ql_secret_ensure omnigent-database-url env:OMNIGENT_DATABASE_URL_VALUE --update
  # shellcheck disable=SC2034 # cleared on purpose; ql_secret_ensure already read it
  OMNIGENT_DATABASE_URL_VALUE=''
  [[ ${QL_SECRET_CHANGED:-0} != 1 ]] || db_url_changed=1
else
  ql_info "[dry-run] would derive the secret omnigent-database-url from omnigent-postgres-password"
fi

# ---- 6. install changed files, then start / restart only what changed -----------------------
changed=$(ql_install_files "$WORK/out" "$APP" --prune)
[[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
if [[ $DRY == 1 ]]; then
  ql_info "dry-run complete; nothing was changed"
  exit 0
fi
((built == 0)) || ql_mark_changed "$APP" omnigent-runner.service
((db_url_changed == 0)) || ql_mark_changed "$APP" omnigent-server.service
if ((no_start)); then
  systemctl --user daemon-reload
  ql_info "installed; not started (--no-start). Start with: systemctl --user start ${UNITS[*]}"
  exit 0
fi
ql_apply_units "$APP" "${UNITS[@]}"

# ---- 7. first-boot admin claim, health and smoke ----------------------------------------------
BASE=http://$BIND:$PORT
ql_wait_http "$BASE/health" 200 300 || ql_die "$BASE/health did not answer 200; see: journalctl --user -u omnigent-server -n 100"

# Upstream reports needs_setup=true until POST /auth/setup creates the first admin. Claim it
# right away with the secret (piped: never in argv or on the terminal); the server is on
# loopback, so the unauthenticated setup window is this host only, for seconds.
info=$(curl -s -m 10 "$BASE/v1/info" 2>/dev/null || true)
# `.needs_setup // "unknown"` would report "unknown" for a literal false: jq's // treats
# false as empty. Compare explicitly.
needs=$(jq -r 'if .needs_setup == true then "true" elif .needs_setup == false then "false" else "unknown" end' \
  <<<"${info:-null}" 2>/dev/null || echo unknown)
case $needs in
  false) ql_info "admin already claimed (needs_setup=false)" ;;
  true)
    ql_info "first boot (needs_setup=true): claiming admin '$USER_NAME' with the omnigent-admin-password secret"
    code=$(podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password \
      | jq -Rn --arg u "$USER_NAME" '{username: $u, password: input}' \
      | curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST -H 'Content-Type: application/json' \
        --data @- "$BASE/auth/setup" 2>/dev/null) || code=000
    case $code in
      200 | 201) ql_info "admin '$USER_NAME' created" ;;
      409) ql_info "an admin already exists; nothing to claim" ;;
      *) ql_die "POST /auth/setup returned HTTP $code; create the admin '$USER_NAME' in the web UI with the password from: podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password" ;;
    esac
    ;;
  *)
    # Not fatal on its own: tests/smoke.sh below asserts needs_setup=false, so a stack that
    # really has no admin still fails the install, with a clearer error than this one.
    ql_warn "cannot read needs_setup from $BASE/v1/info (got: ${info:0:120})"
    ql_warn "if the web UI asks you to create the first admin, use '$USER_NAME' with the password from:"
    printf "    podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password\n" >&2
    ;;
esac

# podman's transient health timers are not reliable for postgres (see the health service):
# run the checks actively while waiting.
QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-postgres 180 \
  || ql_die "omnigent-postgres did not become healthy; see: journalctl --user -u omnigent-postgres -n 100"
QL_HEALTH_ACTIVE=1 ql_wait_container_healthy omnigent-server 180 \
  || ql_die "omnigent-server did not become healthy; see: journalctl --user -u omnigent-server -n 100"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed; see the output above"

pi_hint="podman exec -it omnigent-runner pi login   (once: the private pi volume starts empty)"
[[ $PI_STATE == private ]] || pi_hint="shared with pi-web (pi-agent-data): log in there"
cat >&2 <<EOF

$APP $VERSION is installed and healthy.

  Web UI       $BASE/   (put a tailnet or tunnel front end on this host in front; README "Tailnet HTTPS")
  Admin        $USER_NAME; password: podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password
  pi           $pi_hint
  Logs         journalctl --user -u omnigent-server -f   |   podman logs -f omnigent-runner
  Settings     $ENV_FILE (edit, then re-run $0)

EOF
