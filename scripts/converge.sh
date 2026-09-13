#!/usr/bin/env bash
# scripts/converge.sh: bring a HAND-INSTALLED omnigent Quadlet deployment (woowtechopenclaw)
# onto the units this repo ships, with the evidence an operator needs around it.
#
#   scripts/converge.sh [--check] [--yes] [--no-auto-rollback] [--pi-state private|shared]
#   scripts/converge.sh --rollback [--yes]
#   scripts/converge.sh --status
#
#   --check        pre-flight + drift report + a dry run of install.sh. It creates the two
#                  podman secrets if they are missing (additive; install.sh refuses to render
#                  anything without them) and changes nothing else: no unit file, no container.
#   --pi-state M   override the mode derived from the running runner (private | shared)
#   --no-auto-rollback  leave a failed converge in place for inspection
#   --rollback     put the previous unit files back and restart the stack on them
#   --status       print what the last converge recorded
#
# THIS IS NOT A MIGRATION, and there is deliberately no migrate-legacy.sh here. The stack is
# already Quadlet: the container names, the volumes (omnigent-postgres-data,
# omnigent-server-data) and the network (omnigent) that openclaw's hand-written units declare
# are exactly the ones this repo declares, so there is nothing to adopt by rename and no legacy
# container to keep. Converging IS `scripts/install.sh`: ql_install_files backs up each foreign
# file with our name before writing ours, and ql_apply_units restarts only the units whose file
# actually changed. That is the path Woow_podman_pi_agent_package took on toypark1234 (a
# hand-edited pi-web.container with literal /home/<user> paths, repointed at %h/%t for 1.5 s of
# downtime), and its README calls it "Converging a hand-edited install".
#
# What this wrapper adds around that one command:
#   1 a pre-flight that refuses instead of guessing
#   2 a named report of WHAT drifted, per file, before anything is restarted
#   3 a checksummed backup taken first: pg_dump, both volume exports, the unit files, the
#     podman inspects
#   4 the secret adoption openclaw needs - its POSTGRES_PASSWORD and OMNIGENT_ADMIN_PASSWORD
#     are spelled into the unit files, and the repo's units read podman secrets instead. An
#     adopted Postgres volume only opens with the password it was initialised with, so this has
#     to happen before install.sh, and install.sh refuses to run without it (by design).
#   5 proof that the data was adopted: the CreatedAt and mountpoint inode of every volume are
#     recorded before and asserted after
#   6 a measured downtime, sampled from the outside every 100 ms
#   7 --rollback: the saved unit files back in place, daemon-reload, restart. The old image
#     tags (postgres:16-alpine, omnigent-server:latest, woow-omnigent-runner:latest) are still
#     on the host; nothing here removes an image.
#
# pi-agent-data: in shared mode the runner mounts the volume of Woow_podman_pi_agent_package,
# which pi-web - a live service on both hosts - also mounts. This script NEVER installs,
# restarts, stops or removes pi-agent-data.volume or pi-web: it only references the unit, and
# it records pi-web's container id and the volume's identity before and after so a converge
# that disturbed either is visible rather than assumed away.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=converge-lib.sh
. "$REPO/scripts/converge-lib.sh"

# ---- per-repo settings -------------------------------------------------------------------
CV_APP=omnigent
CV_STATE_DIR=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$CV_APP
CV_STATE=$CV_STATE_DIR/converge.state
CV_BACKUP_ROOT=$HOME/backups/$CV_APP
ENV_FILE=$HOME/.config/$CV_APP/$CV_APP.env
DOC_URL='Documentation=https://github.com/WOOWTECH/Woow_podman_omnigent_package'
CONTAINERS=(omnigent-postgres omnigent-server omnigent-runner)
OWN_VOLUMES=(omnigent-postgres-data omnigent-server-data)
SHARED_VOLUME=pi-agent-data
SHARED_VOLUME_OWNER=pi-web
PLAIN_UNITS=(omnigent-server-health.service omnigent-server-health.timer)
DB_SECRET=omnigent-postgres-password
ADMIN_SECRET=omnigent-admin-password
PODMAN_MIN=4.9
# ------------------------------------------------------------------------------------------

mode=converge pi_state='' auto_rollback=1 ASSUME_YES=0
while (($#)); do
  case $1 in
    --check) mode=check ;;
    --pi-state) pi_state=${2:?--pi-state needs private or shared}; shift ;;
    --pi-state=*) pi_state=${1#--pi-state=} ;;
    --no-auto-rollback) auto_rollback=0 ;;
    --rollback) mode=rollback ;;
    --status) mode=status ;;
    --yes) ASSUME_YES=1 ;;
    -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$CV_APP
confirm() {
  ((!ASSUME_YES)) || return 0
  [[ -t 0 ]] || ql_die "$1 (not a terminal: pass --yes)"
  local a
  read -r -p "$1. Continue? [y/N] " a
  [[ $a == [yY]* ]] || ql_die "aborted"
}

if [[ $mode == status ]]; then
  if [[ -f $CV_STATE ]]; then cat "$CV_STATE"; else echo "no converge recorded in $CV_STATE"; fi
  exit 0
fi

ql_preflight "$PODMAN_MIN"
ql_lock "$CV_APP"
export WOOW_QL_LOCK_HELD=$CV_APP

# adopt_secrets: create the two podman secrets from the values the hand-written units spell
# out, unless they already exist. Additive and idempotent: it creates podman secrets and
# touches no service, no unit file and no container - which is why --check runs it too. It has
# to happen BEFORE install.sh in either mode, because install.sh refuses to run when the
# Postgres volume exists and the secret that opens it does not (that refusal is correct: the
# role's password was set by initdb on the volume being adopted, and only a secret holding that
# password can open it). Without this, `--check` could not even reach the dry-run.
adopt_secrets() {
  ql_info "adopting the passwords the hand-written units spell out into podman secrets"
  # The Postgres role's password was set by initdb on the volume that is being adopted: only a
  # secret holding THAT password can open it. Read it out of the running container and pipe it in
  # - it never reaches argv, the journal or xtrace.
  if podman secret exists "$DB_SECRET" 2>/dev/null; then
    ql_info "$DB_SECRET already exists; keeping it"
  else
    CONVERGE_SECRET_VALUE=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-postgres \
      | sed -n 's/^POSTGRES_PASSWORD=//p' | tail -n1)
    [[ -n $CONVERGE_SECRET_VALUE ]] \
      || ql_die "omnigent-postgres has no POSTGRES_PASSWORD in its environment and the secret $DB_SECRET does not exist: the adopted volume could not be opened. Create the secret by hand from the password the database really has, then re-run"
    [[ $CONVERGE_SECRET_VALUE =~ ^[A-Za-z0-9._~-]+$ ]] \
      || ql_die "the current database password contains characters that need URL encoding in DATABASE_URL; rotate it with scripts/rotate-secrets.sh --db after the converge, and create $DB_SECRET by hand for now"
    # shellcheck disable=SC2034 # read by ql_secret_ensure through env:CONVERGE_SECRET_VALUE
    ql_secret_ensure "$DB_SECRET" env:CONVERGE_SECRET_VALUE
    ql_info "adopted $DB_SECRET from omnigent-postgres (not printed)"
  fi
  if podman secret exists "$ADMIN_SECRET" 2>/dev/null; then
    ql_info "$ADMIN_SECRET already exists; keeping it"
  else
    CONVERGE_SECRET_VALUE=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-runner \
      | sed -n 's/^OMNIGENT_ADMIN_PASSWORD=//p' | tail -n1)
    if [[ -n $CONVERGE_SECRET_VALUE ]]; then
      # shellcheck disable=SC2034 # read by ql_secret_ensure through env:CONVERGE_SECRET_VALUE
      ql_secret_ensure "$ADMIN_SECRET" env:CONVERGE_SECRET_VALUE
      ql_info "adopted $ADMIN_SECRET from omnigent-runner (not printed)"
    else
      ql_die "omnigent-runner has no OMNIGENT_ADMIN_PASSWORD and the secret $ADMIN_SECRET does not exist: the runner would not be able to log in after the converge. Create the secret from the admin account's real password first"
    fi
  fi
  CONVERGE_SECRET_VALUE=''
  unset CONVERGE_SECRET_VALUE
}

# =============================================================================================
# rollback
# =============================================================================================
if [[ $mode == rollback ]]; then
  bk=$(cv_state_get BACKUP)
  [[ -n $bk && -d $bk ]] || ql_die "no converge backup recorded in $CV_STATE"
  [[ $(cv_state_get STATUS) == converged || $(cv_state_get STATUS) == failed ]] \
    || ql_die "nothing to roll back (status: $(cv_state_get STATUS))"
  confirm "--rollback puts the unit files from $bk/units back and restarts omnigent on them"
  t0=$(cv_now_ms)
  cv_restore_units "$bk"
  systemctl --user daemon-reload
  # The restored units carry Environment=POSTGRES_PASSWORD / DATABASE_URL again, so they do not
  # need the secrets; the secrets are left in place (harmless, and a re-converge reuses them).
  systemctl --user restart omnigent-postgres.service
  systemctl --user restart omnigent-server.service
  systemctl --user restart omnigent-runner.service
  ql_wait_http "http://$(cv_state_get BIND):$(cv_state_get PORT)/health" '200' 300 \
    || ql_die "omnigent did not answer /health after the rollback"
  t1=$(cv_now_ms)
  cv_state_set STATUS rolled-back
  ql_info "rolled back in $(((t1 - t0) / 1000)).$(printf '%03d' $(((t1 - t0) % 1000))) s; the previous unit files serve again"
  ql_info "the state directory still records this package as the owner of those files; re-run scripts/converge.sh to go forward again"
  exit 0
fi

# =============================================================================================
# 1. pre-flight (read-only)
# =============================================================================================
ql_info "step 1/5: pre-flight"
for c in "${CONTAINERS[@]}"; do
  podman container exists "$c" || ql_die "container $c does not exist: this host does not run omnigent, so there is nothing to converge (a fresh host runs scripts/install.sh)"
  label=$(podman inspect --format '{{index .Config.Labels "PODMAN_SYSTEMD_UNIT"}}' "$c" 2>/dev/null || true)
  [[ $label == "${c}.service" ]] \
    || ql_die "container $c is not managed by the Quadlet unit $c.service (PODMAN_SYSTEMD_UNIT='$label'); that is a migration, not a converge - resolve it by hand"
  cv_running "$c" || ql_die "container $c is not running; start the stack first, so the converge can read its settings and prove the data is adopted"
done
for v in "${OWN_VOLUMES[@]}"; do
  podman volume exists "$v" || ql_die "volume $v does not exist, but $((${#CONTAINERS[@]})) omnigent containers are running: refusing to guess where the data is"
done

# The mode the runner is actually in, read from its mount rather than assumed.
runner_vol=$(podman inspect --format '{{range .Mounts}}{{if eq .Destination "/data/pi-agent"}}{{.Name}}{{end}}{{end}}' omnigent-runner)
[[ -n $runner_vol ]] || ql_die "omnigent-runner has no volume at /data/pi-agent; this converge does not know that shape"
case $runner_vol in
  "$SHARED_VOLUME") derived_state=shared ;;
  omnigent-pi-data) derived_state=private ;;
  *) ql_die "omnigent-runner mounts the unexpected volume '$runner_vol' at /data/pi-agent; name the mode with --pi-state after checking what that volume is" ;;
esac
pi_state=${pi_state:-$derived_state}
[[ $pi_state == private || $pi_state == shared ]] || ql_die "--pi-state must be private or shared"
[[ $pi_state == "$derived_state" ]] \
  || ql_warn "--pi-state $pi_state differs from what omnigent-runner mounts today ($derived_state via '$runner_vol'): the runner will lose sight of the other volume's pi sessions"
QDIR=$CV_QDIR
if [[ $pi_state == shared ]]; then
  [[ -f $QDIR/$SHARED_VOLUME.volume ]] \
    || ql_die "shared pi state needs $QDIR/$SHARED_VOLUME.volume from Woow_podman_pi_agent_package; install that package first, or converge with --pi-state private"
  podman volume exists "$SHARED_VOLUME" || ql_die "volume $SHARED_VOLUME does not exist"
  # Recorded, never touched. pi-web is a live service that mounts the same volume; this script
  # must not install, restart or remove that volume unit, and the numbers below are how a
  # reviewer sees that it did not.
  SHARED_ID=$(cv_volume_identity "$SHARED_VOLUME") || ql_die "cannot read the identity of $SHARED_VOLUME"
  PIWEB_ID=$(podman inspect --format '{{.Id}} {{.State.StartedAt}}' "$SHARED_VOLUME_OWNER" 2>/dev/null || echo 'not-on-this-host')
  ql_info "shared pi state: $SHARED_VOLUME is $SHARED_ID and is also mounted by $SHARED_VOLUME_OWNER ($PIWEB_ID)"
  ql_info "this converge never installs, restarts or removes $SHARED_VOLUME.volume or $SHARED_VOLUME_OWNER.service"
fi

# Publish address and port, read from the running server rather than from the repo's defaults.
pub=$(podman inspect --format '{{range $p, $b := .NetworkSettings.Ports}}{{$p}}|{{range $b}}{{.HostIP}}:{{.HostPort}} {{end}}{{println}}{{end}}' omnigent-server) \
  || ql_die "cannot read the published ports of omnigent-server"
row=$(sed -n 's#^8000/tcp|##p' <<<"$pub" | tr ' ' '\n' | grep -m1 ':') \
  || ql_die "omnigent-server publishes no host port for 8000/tcp"
BIND=${row%:*} PORT=${row##*:}
[[ -n $BIND ]] || BIND=0.0.0.0
[[ $BIND != 0.0.0.0 ]] \
  || ql_die "omnigent-server publishes on 0.0.0.0:$PORT; install.sh refuses that (the first visitor to /auth/setup owns the instance). Re-publish on one address first, or pass --set OMNIGENT_BIND=<addr> to install.sh yourself"
BASE_URL=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-server | sed -n 's/^OMNIGENT_ACCOUNTS_BASE_URL=//p' | tail -n1)
ADMIN_USER=$(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-runner | sed -n 's/^OMNIGENT_ADMIN_USERNAME=//p' | tail -n1)
ADMIN_USER=${ADMIN_USER:-admin}
ql_info "omnigent-server publishes $BIND:$PORT, base URL '${BASE_URL:-<empty>}', admin '$ADMIN_USER', pi state $pi_state"

# =============================================================================================
# 2. drift report
# =============================================================================================
ql_info "step 2/5: what drifted (installed files vs this repo's shape)"
OWN_FILES=(omnigent-postgres.container omnigent-server.container omnigent-runner.container
  omnigent.network omnigent-postgres.volume omnigent-server.volume
  omnigent-server-health.service omnigent-server-health.timer)
[[ $pi_state != private ]] || OWN_FILES+=(omnigent-pi.volume)
drifted=0
cv_drift_report "${OWN_FILES[@]}" || drifted=1
if ((!drifted)); then
  ql_info "nothing drifted. install.sh is still the right command to run: it will report 0 changed files and restart nothing"
fi

if [[ $mode == check ]]; then
  ql_info "step 3/5 (--check): adopting the passwords into podman secrets, then install.sh --dry-run"
  adopt_secrets
  mapfile -t to_adopt < <(cv_plain_units_to_adopt "$CV_APP" "${PLAIN_UNITS[@]}")
  if ((${#to_adopt[@]})); then
    ql_info "hand-installed helper units the converge will take over (a copy is kept under the state dir):"
    printf '    %s\n' "${to_adopt[@]}" >&2
  fi
  # The dry-run gets a scratch plain-unit directory. Those helper units are ours but in no
  # manifest yet, so install.sh's shadow guard would refuse to run and validate nothing - and
  # moving them aside is a real change that --check must not make. Under --dry-run nothing is
  # written anywhere, so the scratch directory only silences a guard whose answer this script
  # already knows and just printed.
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/$CV_APP-check.XXXXXX")
  QL_SYSTEMD_USER_DIR=$scratch "$REPO/scripts/install.sh" --dry-run --pi-state "$pi_state" \
    --set "OMNIGENT_BIND=$BIND" --set "OMNIGENT_PORT=$PORT" \
    --set "OMNIGENT_ADMIN_USERNAME=$ADMIN_USER" --set "OMNIGENT_ACCOUNTS_BASE_URL=$BASE_URL" \
    || { rm -rf "$scratch"; ql_die "install.sh --dry-run failed; the converge would not have got past this point"; }
  rm -rf "$scratch"
  ql_info "--check complete; no unit file, container or service was changed"
  exit 0
fi

# =============================================================================================
# 3. backup first, then adopt the secrets the hand-written units spell out
# =============================================================================================
confirm "the converge restarts the omnigent containers whose unit files change"
ql_info "step 3/5: backup (pg_dump, volume exports, unit files, inspects) with checksums"
bk=$(cv_new_backup_dir "$CV_BACKUP_ROOT/converge-$(date +%Y%m%d-%H%M%S)")
cv_backup_units "$bk" "${OWN_FILES[@]}"
podman inspect "${CONTAINERS[@]}" >"$bk/inspect.json"
chmod 600 -- "$bk/inspect.json"
[[ ! -f $ENV_FILE ]] || cp -p -- "$ENV_FILE" "$bk/omnigent.env"
# A logical dump, not only the volume export: the export of a live PGDATA is a torn copy, and
# this is the one piece of state a bad converge could make unopenable.
(umask 077 && podman exec omnigent-postgres pg_dump -U omnigent -d omnigent -Fc >"$bk/omnigent.pgdump.partial") \
  || { rm -f -- "$bk/omnigent.pgdump.partial"; ql_die "pg_dump of the omnigent database failed; nothing was changed"; }
mv -f -- "$bk/omnigent.pgdump.partial" "$bk/omnigent.pgdump"
(umask 077 && podman exec omnigent-postgres pg_dumpall -U omnigent --roles-only >"$bk/roles.sql") \
  || ql_warn "pg_dumpall --roles-only failed; the dump above is still there"
for v in "${OWN_VOLUMES[@]}"; do ql_backup_volume "$v" "$bk" >/dev/null; done
{
  printf 'publish: %s:%s\n' "$BIND" "$PORT"
  printf 'pi state: %s (runner mounts %s)\n' "$pi_state" "$runner_vol"
  printf 'admin: %s ; base url: %s\n' "$ADMIN_USER" "${BASE_URL:-<empty>}"
  for v in "${OWN_VOLUMES[@]}"; do printf 'volume %s: %s\n' "$v" "$(cv_volume_identity "$v")"; done
  [[ $pi_state != shared ]] || printf 'volume %s (NOT ours): %s\n' "$SHARED_VOLUME" "$SHARED_ID"
  [[ $pi_state != shared ]] || printf '%s: %s\n' "$SHARED_VOLUME_OWNER" "$PIWEB_ID"
  printf 'health before: %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://$BIND:$PORT/health" || echo 000)"
} >"$bk/precheck.txt"
chmod 600 -- "$bk/precheck.txt"
cv_state_set BACKUP "$bk"
cv_state_set BIND "$BIND"
cv_state_set PORT "$PORT"
cv_state_set PI_STATE "$pi_state"
for v in "${OWN_VOLUMES[@]}"; do cv_state_set "VOLID_$v" "$(cv_volume_identity "$v")"; done
cv_write_checksums "$bk"
ql_info "backup in $bk (verify with: cd $bk && sha256sum -c SHA256SUMS)"

adopt_secrets

# The hand-installed plain health units are ours but in no manifest: move them aside so
# ql_install_files may write this repo's versions instead of refusing them as foreign.
mkdir -p "$bk/rendered"
cp -p "$REPO"/systemd/omnigent-server-health.service "$REPO"/systemd/omnigent-server-health.timer "$bk/rendered/"
cv_adopt_plain_units "$CV_APP" "$bk/rendered" "$DOC_URL" "${PLAIN_UNITS[@]}"

# =============================================================================================
# 4. the converge itself: scripts/install.sh
# =============================================================================================
ql_info "step 4/5: scripts/install.sh (the converge; it restarts only the units whose file changed)"
probe=$bk/downtime-probe.log
cv_probe_start "http://$BIND:$PORT/health" "$probe"
T0=$(cv_now_ms)
failed=0
"$REPO/scripts/install.sh" --pi-state "$pi_state" \
  --set "OMNIGENT_BIND=$BIND" --set "OMNIGENT_PORT=$PORT" \
  --set "OMNIGENT_ADMIN_USERNAME=$ADMIN_USER" --set "OMNIGENT_ACCOUNTS_BASE_URL=$BASE_URL" \
  --yes 2>&1 | tee "$bk/install.log" || failed=1
T1=$(cv_now_ms)
sleep 2 # let the probe record the first successes after the restart
cv_probe_stop

# =============================================================================================
# 5. verify: the data is the SAME data, and nothing else moved
# =============================================================================================
if ((!failed)); then
  ql_info "step 5/5: verifying that the volumes were adopted and the shared one was not touched"
  for v in "${OWN_VOLUMES[@]}"; do
    now=$(cv_volume_identity "$v") || now='unreadable'
    if [[ $now == "$(cv_state_get "VOLID_$v")" ]]; then
      ql_info "  $v adopted: $now"
    else
      ql_warn "  $v CHANGED: before='$(cv_state_get "VOLID_$v")' after='$now' - a new CreatedAt or inode means a fresh empty volume, not the data"
      failed=1
    fi
  done
  if [[ $pi_state == shared ]]; then
    now=$(cv_volume_identity "$SHARED_VOLUME") || now='unreadable'
    [[ $now == "$SHARED_ID" ]] || { ql_warn "  $SHARED_VOLUME changed: '$SHARED_ID' -> '$now'"; failed=1; }
    now=$(podman inspect --format '{{.Id}} {{.State.StartedAt}}' "$SHARED_VOLUME_OWNER" 2>/dev/null || echo 'not-on-this-host')
    if [[ $now == "$PIWEB_ID" ]]; then
      ql_info "  $SHARED_VOLUME_OWNER untouched: same container id and the same StartedAt ($now)"
      ql_info "  $SHARED_VOLUME untouched: $SHARED_ID"
    else
      ql_warn "  $SHARED_VOLUME_OWNER was disturbed: '$PIWEB_ID' -> '$now'"
      failed=1
    fi
  fi
fi
changed_files=$(sed -n 's/^.*changed: //p' "$bk/install.log" | tail -n1)
restarted=$(grep -cE 'restarting |starting ' "$bk/install.log" || true)
down=$(cv_probe_downtime_ms "$probe")
cv_write_checksums "$bk"

if ((failed)); then
  cv_state_set STATUS failed
  if ((auto_rollback)); then
    ql_warn "the converge failed; rolling back automatically (--no-auto-rollback keeps it for inspection)"
    ASSUME_YES=1 exec "$0" --rollback --yes
  fi
  ql_die "the converge failed; the new units are in place. Inspect, then run: $0 --rollback"
fi
cv_state_set STATUS converged
cv_state_set DOWNTIME_MS "$down"
cv_state_set CHANGED "${changed_files:-none}"
printf '\n' >&2
ql_info "converged."
ql_info "  files changed : ${changed_files:-none}"
ql_info "  units touched : $restarted start/restart line(s) in $bk/install.log"
if ((down < 0)); then
  ql_warn "  downtime      : the probe never saw a successful sample; check $probe by hand"
else
  ql_info "  downtime      : ${down} ms (probe every 100 ms against http://$BIND:$PORT/health), wall clock $(((T1 - T0) / 1000)) s"
fi
ql_info "  backup        : $bk (sha256sum -c SHA256SUMS)"
ql_info "  rollback      : $0 --rollback"
ql_info "run $0 again: it must report 'files changed : none' and take no downtime. That is the property that says the host and the repo now agree."
ql_warn "the adopted passwords were in the old unit files, i.e. in plain text on disk and in every backup of them: rotate with scripts/rotate-secrets.sh --all"
