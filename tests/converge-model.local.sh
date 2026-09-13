# shellcheck shell=bash
# tests/converge-model.local.sh: omnigent-specific assertions, sourced at the end of
# tests/converge-model.sh (which defines REPO, run, npass, nfail and FAILED).
#
# These are structural: they read scripts/converge.sh and scripts/install.sh rather than run
# them, because the property they pin is "this code never does X to a volume another live
# service owns", and the only honest way to check a negative is to look for it.
# shellcheck disable=SC2154 # REPO, npass, nfail, FAILED come from tests/converge-model.sh
# shellcheck disable=SC2016 # the greps look for literal shell text in another script

local_ok() { npass=$((npass + 1)); printf 'ok    %s\n' "$1"; }
local_fail() { nfail=$((nfail + 1)); FAILED+=("$1"); printf 'FAIL  %s\n      | %s\n' "$1" "$2"; }
local_check() { if [[ -z $2 ]]; then local_ok "$1"; else local_fail "$1" "$2"; fi; }

# pi-agent-data is mounted by pi-web, a live service on toypark1234 AND on woowtechopenclaw.
# omnigent references its .volume unit in shared mode; it must never install, restart, stop or
# remove that unit, and never remove the volume.
msg=''
grep -nE 'systemctl --user (restart|stop|start|disable|enable).*(pi-web|pi-agent)' "$REPO/scripts/converge.sh" \
  && msg='converge.sh acts on a pi-agent unit'
grep -nE 'podman volume (rm|prune)' "$REPO/scripts/converge.sh" "$REPO/scripts/install.sh" \
  && msg="$msg; a script removes a volume"
grep -nE 'podman (system prune|system reset|stop -a|rm -a)' "$REPO/scripts/converge.sh" "$REPO/scripts/install.sh" \
  && msg="$msg; a script runs an unqualified bulk command"
local_check t_local_converge_never_touches_the_shared_pi_volume "$msg"

# The repo must not ship a pi-agent-data.volume of its own: that file belongs to
# Woow_podman_pi_agent_package, and two packages installing the same Quadlet file would fight
# over it through their manifests.
msg=''
[[ -e $REPO/quadlet/pi-agent-data.volume ]] && msg='the repo ships a pi-agent-data.volume'
grep -q 'pi-agent-data.volume' "$REPO/scripts/render-args.sh" || msg="$msg; shared mode does not reference pi-agent-data.volume"
grep -q 'omnigent-pi.volume' "$REPO/scripts/install.sh" || msg="$msg; private mode does not install its own volume"
local_check t_local_shared_mode_only_references_the_other_packages_volume "$msg"

# install.sh copies the files it installs one by one; pi-agent-data.volume must not be in that
# list in either mode, or ql_install_files would claim ownership of pi-web's file.
msg=''
grep -nE '^\s*(cp|install) .*pi-agent-data' "$REPO/scripts/install.sh" && msg='install.sh stages pi-agent-data.volume'
local_check t_local_install_never_stages_the_pi_agent_volume "$msg"

# The database is the one thing a bad converge could make unopenable: a logical dump, not only
# a volume export, and the password of the adopted volume is adopted rather than regenerated.
msg=''
grep -q 'pg_dump' "$REPO/scripts/converge.sh" || msg='converge.sh takes no pg_dump'
grep -q 'POSTGRES_PASSWORD=//p' "$REPO/scripts/converge.sh" || msg="$msg; the db password is not adopted from the running container"
grep -q 'cv_volume_identity' "$REPO/scripts/converge.sh" || msg="$msg; the volumes are not fingerprinted"
grep -q 'cv_write_checksums' "$REPO/scripts/converge.sh" || msg="$msg; the backup is not checksummed"
grep -q 'DOWNTIME_MS' "$REPO/scripts/converge.sh" || msg="$msg; the downtime is not recorded"
local_check t_local_the_database_is_dumped_and_its_password_adopted "$msg"

# Found on toypark1234 against quadlet-lib 1.4.0: converge.sh takes ql_lock and then runs
# install.sh, whose own ql_lock aborted the run, so install.sh was taught to skip the lock on a
# private WOOW_QL_LOCK_HELD flag. 1.5.0 resolves the nesting in the library instead - ql_lock
# exports QL_LOCK_HELD and a nested ql_lock keeps the caller's lock, pinned behaviourally by
# t_a_child_script_reuses_the_lock_its_caller_holds - so install.sh locks unconditionally again.
# The flag must not come back: it is not a no-op like app_unlocked, it silently stops install.sh
# locking at all whenever it is stale in the environment.
msg=''
grep -qF 'ql_lock "$APP"' "$REPO/scripts/install.sh" \
  || msg='install.sh no longer takes the app lock at all'
grep -q 'WOOW_QL_LOCK_HELD' "$REPO/scripts/install.sh" \
  && msg="$msg; install.sh still skips ql_lock on a private flag"
grep -qF 'ql_lock "$CV_APP"' "$REPO/scripts/converge.sh" \
  || msg="$msg; converge.sh does not take the lock before it calls install.sh"
grep -q 'WOOW_QL_LOCK_HELD' "$REPO/scripts/converge.sh" \
  && msg="$msg; converge.sh still exports the obsolete lock flag"
local_check t_local_install_sh_locks_and_the_library_resolves_the_nesting "${msg#; }"

# --check must be able to reach install.sh --dry-run. Found on toypark1234: install.sh refuses
# to render while the Postgres volume exists and the secret that opens it does not, so --check
# died before validating anything until the secret adoption became a function both modes call.
msg=''
grep -q '^adopt_secrets()' "$REPO/scripts/converge.sh" \
  || msg='the secret adoption is not a function both modes can call'
awk '/mode == check/{c=1} c && /adopt_secrets/{a=1} c && a && /install.sh" --dry-run/{ok=1} END{exit !ok}' \
  "$REPO/scripts/converge.sh" || msg="$msg; --check does not adopt the secrets before the dry-run"
local_check t_local_check_can_reach_the_dry_run "${msg#; }"

# --check must validate the render on a host whose plain helper units were installed by hand.
# Found on toypark1234: they are ours but in no manifest, so install.sh's shadow guard refused
# to run and --check validated nothing. Moving them aside is a real change --check must not
# make, so the dry-run gets a scratch QL_SYSTEMD_USER_DIR and the units are NAMED instead.
msg=''
grep -q 'cv_plain_units_to_adopt' "$REPO/scripts/converge.sh" \
  || msg='--check does not name the hand-installed helper units it will take over'
grep -q 'QL_SYSTEMD_USER_DIR=$scratch "$REPO/scripts/install.sh" --dry-run' "$REPO/scripts/converge.sh" \
  || msg="$msg; the dry-run does not get a scratch plain-unit directory"
local_check t_local_check_survives_hand_installed_helper_units "${msg#; }"

# A re-run that changed nothing still takes a fresh backup, whose units/ holds the ALREADY
# CONVERGED files. If --rollback followed the newest backup, the second (no-op) run would
# silently destroy the only way back to the pre-converge units. Found on toypark1234.
msg=''
# -A1: the assertion is about the ROLLBACK BLOCK, not about the word appearing anywhere.
grep -A1 -F 'bk=$(cv_state_get ROLLBACK_BACKUP)' "$REPO/scripts/converge.sh" \
  | grep -qF 'bk=$(cv_state_get BACKUP)' \
  || msg='--rollback does not resolve a dedicated rollback point (with the old key as a fallback)'
grep -qF '[[ -z $changed_files ]] || cv_state_set ROLLBACK_BACKUP "$bk"' "$REPO/scripts/converge.sh" \
  || msg="$msg; a run that changed nothing still repoints the rollback"
local_check t_local_a_no_op_rerun_does_not_move_the_rollback_point "${msg#; }"
