#!/usr/bin/env bash
# scripts/backup.sh: back up omnigent into a new directory.
#
#   scripts/backup.sh [--dest DIR] [--include-secrets] [--stop]
#
#   --dest DIR          parent directory (default ~/backups/omnigent); a <timestamp>/
#                       subdirectory is created in it and printed on stdout
#   --include-secrets   also write the podman secrets to secrets.json (0600). Needed to
#                       restore onto another host: the database dump does not carry the
#                       role password, and the admin password hash in it only matches the
#                       admin and operator secrets of the same moment.
#   --stop              stop omnigent-runner and omnigent-server before the database dump
#                       and start them again afterwards. Without it the two volumes are
#                       tarred while their containers write them, which is what podman's
#                       "the export may be inconsistent" warning is about. PostgreSQL stays
#                       running for pg_dump; --stop keeps the dump and artifacts at the
#                       same application checkpoint.
#
# Contents: omnigent-<ts>.dump (pg_dump -Fc: accounts, policies, sessions), an export of
# omnigent-server-data, an export of omnigent-pi-data in private mode, and a copy of the env
# file plus installed units/checkpoint hashes. Everything is 0600 in a 0700 directory.
# Restore requires those exact units/env installed first; see docs/mcp-upgrade-rollback.md.
# pi-agent-data (shared mode) belongs to Woow_podman_pi_agent_package and is not backed up here.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=writer-state.sh
. "$REPO/scripts/writer-state.sh"
umask 077
APP=omnigent
ENV_FILE=$HOME/.config/$APP/$APP.env
dest=$HOME/backups/$APP include_secrets=0 stop=0
while (($#)); do
  case $1 in
    --dest) dest=${2:?--dest needs a directory}; shift ;;
    --include-secrets) include_secrets=1 ;;
    --stop) stop=1 ;;
    -h | --help) sed -n '2,22p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
ql_lock "$APP"
[[ $(podman inspect --format '{{.State.Status}}' omnigent-postgres 2>/dev/null || true) == running ]] \
  || ql_die "omnigent-postgres is not running; start it (systemctl --user start omnigent-postgres) so pg_dump can run"

base=$dest/$(date +%Y%m%d-%H%M%S) out=$dest/$(date +%Y%m%d-%H%M%S) n=2
while [[ -e $out ]]; do out=$base-$n; n=$((n + 1)); done
(umask 077 && mkdir -p -- "$out") || ql_die "cannot create $out"
chmod 700 "$out"

checkpoint_args=(--backup "$out" --quadlet-dir "$(_ql_quadlet_dir)" --systemd-dir "$(_ql_sd_user_dir)" --env "$ENV_FILE")
python3 "$REPO/scripts/backup-checkpoint.py" capture "${checkpoint_args[@]}" \
  || ql_die "cannot capture matching installed units/config; no writers stopped"
if ((include_secrets)); then
  python3 "$REPO/scripts/backup-checkpoint.py" save-secrets --backup "$out" \
    || ql_die "cannot capture all required secrets; no writers stopped"
fi

resume_units=()
start_stopped() {
  ((${#resume_units[@]})) || return 0
  local -a again=("${resume_units[@]}")
  resume_units=()
  if ! systemctl --user start "${again[@]}"; then
    ql_warn "could not resume ${again[*]}; run: systemctl --user start ${again[*]}"
    return 1
  fi
}
if ((stop)); then
  for unit in "${OMNIGENT_WRITER_UNITS[@]}"; do
    state=$(systemctl --user show -p ActiveState --value "$unit") || ql_die "cannot read $unit state"
    substate=$(systemctl --user show -p SubState --value "$unit") || ql_die "cannot read $unit substate"
    printf '%s\t%s\t%s\n' "$unit" "$state" "$substate" >>"$out/writer-states.tsv"
    case $state in
      active | activating | reloading) resume_units+=("$unit") ;;
      inactive | failed | deactivating) [[ $substate != auto-restart ]] || resume_units+=("$unit") ;;
      *) ql_die "unknown $unit state: $state" ;;
    esac
  done
  ql_cleanup resume_writers start_stopped
  omnigent_stop_writers
fi

dump=$out/$APP-${out##*/}.dump
if ! (umask 077 && podman exec omnigent-postgres pg_dump -U omnigent -d omnigent -Fc >"$dump.partial"); then
  rm -f -- "$dump.partial"
  ql_die "pg_dump failed; nothing was written"
fi
mv -f -- "$dump.partial" "$dump"
(cd -- "$out" && umask 077 && sha256sum -- "${dump##*/}" >"${dump##*/}.sha256")
ql_info "dumped the database -> $dump ($(du -h -- "$dump" | cut -f1))"

((stop == 0)) || omnigent_assert_writers_stopped
ql_backup_volume omnigent-server-data "$out" >/dev/null
pi_state=private
if [[ -f $ENV_FILE ]]; then
  ql_env_load "$ENV_FILE"
  pi_state=$(ql_env_get OMNIGENT_PI_STATE private)
fi
if [[ $pi_state == private ]]; then
  ((stop == 0)) || omnigent_assert_writers_stopped
  ql_backup_volume omnigent-pi-data "$out" >/dev/null
fi

python3 "$REPO/scripts/backup-checkpoint.py" check "${checkpoint_args[@]}" >/dev/null \
  || ql_die "installed configuration changed during the checkpoint"
start_stopped
ql_cleanup_clear resume_writers
ql_info "backup complete: $out"
printf '%s\n' "$out"
