#!/usr/bin/env bash
# tests/converge-model.sh: pins the converge path of scripts/converge.sh - what counts as
# drift, that the second run changes nothing, that the backup round-trips, and how the
# downtime is measured.
#
#   tests/converge-model.sh [name-filter]
#
# podman and systemctl are the doubles in tests/shims, placed first on PATH; every test gets
# its own HOME and shim state. No container is created, no image is built and the real user
# manager is never touched.
#
# The property that matters most here is the LAST one: a converge is only finished when running
# it again is a no-op. install.sh writes a file only when its bytes differ and restarts a unit
# only when its file changed, so "0 files changed, nothing restarted" on the second run is what
# says the host and the repo now agree. That is exactly how the toypark1234 pi-web repointing
# was verified, and it is the difference between a converge and a re-deploy.
#
# Every test runs in its own subshell on purpose (isolated HOME, shim state, env), so the
# "modified in a subshell" notes do not apply here:
# shellcheck disable=SC2030,SC2031
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO=$(cd "$HERE/.." && pwd -P)
SHIMS=$HERE/shims
FILTER=${1:-}
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/converge-tests.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT
npass=0 nfail=0
FAILED=()

die_t() { printf 'ASSERTION FAILED: %s\n' "$*" >&2; exit 1; }
eq() { [[ $1 == "$2" ]] || die_t "${3:-value}: expected [$2] got [$1]"; }
has() { [[ $1 == *"$2"* ]] || die_t "${3:-output} lacks [$2] in:"$'\n'"$1"; }
hasnt() { [[ $1 != *"$2"* ]] || die_t "${3:-output} must not contain [$2] in:"$'\n'"$1"; }
OUT=''
expect_ok() { OUT=$( ("$@") 2>&1) || die_t "expected success of: $*"$'\n'"$OUT"; }
expect_fail() { if OUT=$( ("$@") 2>&1); then die_t "expected failure of: $*"$'\n'"$OUT"; fi; }

# drifted_unit <path>: a hand-edited unit in the shape woowtechopenclaw really has - literal
# /home/<user> and /run/user/<uid> paths, a floating image tag, AutoUpdate=local, a password
# spelled into the unit, no SuccessExitStatus=143, and a bare volume name instead of a .volume.
drifted_unit() {
  cat >"$1" <<'UNIT'
# a hand-written unit, the openclaw shape
[Unit]
Description=drifted
[Container]
ContainerName=demo
Image=localhost/woow-demo:latest
Pull=never
AutoUpdate=local
PublishPort=0.0.0.0:8443:8080
Volume=/home/woowtechopenclaw/Desktop:/workspace:rw
Volume=/run/user/1000/podman:/run/podman
Volume=demo-data:/data
Environment=POSTGRES_PASSWORD=woowtech
NoNewPrivileges=true
[Service]
Restart=always
[Install]
WantedBy=default.target
UNIT
}
clean_unit() {
  cat >"$1" <<'UNIT'
# the repo's shape: specifiers, a pinned tag, a secret, a .volume reference
[Unit]
Description=clean
[Container]
ContainerName=demo
Image=localhost/woow-demo:1.2.3-1
Pull=never
PublishPort=127.0.0.1:18443:8080
Volume=%h/Desktop:/workspace:rw
Volume=%t/podman:/run/podman
Volume=demo-data.volume:/data
Secret=demo-password,type=env,target=POSTGRES_PASSWORD
NoNewPrivileges=true
[Service]
SuccessExitStatus=143
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
}

# ---- what counts as drift -------------------------------------------------------------------
t_the_openclaw_shape_is_reported_as_drift() {
  drifted_unit "$CV_QDIR/demo.container"
  marks=$(cv_drift_of "$CV_QDIR/demo.container" | LC_ALL=C sort | tr '\n' ' ')
  for m in literal-home literal-runtime floating-tag autoupdate plaintext-secret no-success-exit plain-volume-ref; do
    has "$marks" "$m" "drift marks"
  done
}

t_the_repos_own_shape_has_no_named_drift() {
  clean_unit "$CV_QDIR/demo.container"
  eq "$(cv_drift_of "$CV_QDIR/demo.container")" '' "a unit already in the repo's shape has no drift"
}

t_a_comment_mentioning_a_home_path_is_not_drift() {
  # cv_drift_of must read the settings, not the prose: every one of these units documents the
  # host it came from in a comment, and a report that cried drift over a comment would be
  # ignored within a week.
  printf '# installed on /home/woowtechopenclaw by hand, see /run/user/1000\n[Container]\nImage=x:1.0\nSuccessExitStatus=143\n' >"$CV_QDIR/demo.container"
  # SuccessExitStatus lives in [Service] in a real unit; here it only has to be present
  eq "$(cv_drift_of "$CV_QDIR/demo.container" | grep -c 'literal-')" 0 "comments are not drift"
}

t_the_drift_report_names_every_file_and_fails_only_when_something_drifted() {
  clean_unit "$CV_QDIR/a.container"
  drifted_unit "$CV_QDIR/b.container"
  expect_fail cv_drift_report a.container b.container c.container
  has "$OUT" "a.container" "the report names the clean file"
  has "$OUT" "no named drift" "a file with no named drift is labelled as such, not as identical"
  hasnt "$OUT" " clean" "\"clean\" would claim the file is identical, which this report cannot know"
  has "$OUT" "literal-home" "the drifted file is labelled"
  has "$OUT" "absent" "a file that is not installed yet is labelled"
  # and with nothing drifted it succeeds
  rm -f "$CV_QDIR/b.container"
  clean_unit "$CV_QDIR/b.container"
  expect_ok cv_drift_report a.container b.container
}

# ---- the backup is what --rollback restores --------------------------------------------------
t_the_backup_round_trips_the_installed_units() {
  drifted_unit "$CV_QDIR/demo.container"
  printf '[Unit]\nDescription=helper\n' >"$CV_SYSTEMD_USER_DIR/demo-health.service"
  before=$(sha256sum "$CV_QDIR/demo.container" "$CV_SYSTEMD_USER_DIR/demo-health.service")
  expect_ok cv_backup_units "$T/bk" demo.container demo-health.service
  [[ -f $T/bk/units/demo.container && -f $T/bk/units/demo-health.service ]] || die_t "the backup is incomplete"
  # install.sh replaces them; --rollback must put exactly the old bytes back
  clean_unit "$CV_QDIR/demo.container"
  printf '[Unit]\nDescription=new helper\n' >"$CV_SYSTEMD_USER_DIR/demo-health.service"
  expect_ok cv_restore_units "$T/bk"
  eq "$(sha256sum "$CV_QDIR/demo.container" "$CV_SYSTEMD_USER_DIR/demo-health.service")" "$before" "restored bytes"
}

t_the_backup_checksums_catch_tampering() {
  drifted_unit "$CV_QDIR/demo.container"
  expect_ok cv_backup_units "$T/bk" demo.container
  expect_ok cv_write_checksums "$T/bk"
  (cd "$T/bk" && sha256sum -c SHA256SUMS >/dev/null 2>&1) || die_t "the checksums do not verify"
  printf 'tampered\n' >>"$T/bk/units/demo.container"
  (cd "$T/bk" && sha256sum -c SHA256SUMS >/dev/null 2>&1) && die_t "a tampered backup still verified"
  return 0
}

t_a_rollback_without_a_backup_refuses_instead_of_doing_nothing() {
  expect_fail cv_restore_units "$T/bk"
  has "$OUT" "no saved unit files"
}

# ---- the property that defines a finished converge -------------------------------------------
t_the_second_install_changes_nothing_and_restarts_nothing() {
  # The real ql_install_files and ql_apply_units against the shims. Run 1 is the converge: the
  # hand-edited file is adopted (a backup copy is kept) and the unit is restarted. Run 2 is the
  # same rendered output again: no file is written, nothing is marked pending, nothing restarts.
  mkdir -p "$T/out"
  drifted_unit "$CV_QDIR/demo.container" # what the host has
  clean_unit "$T/out/demo.container"     # what the repo renders
  export QL_APP=demo

  changed1=$(ql_install_files "$T/out" demo) || die_t "the first install failed"
  [[ $changed1 == *demo.container* ]] || die_t "run 1 should have written demo.container, got '$changed1'"
  grep -rq 'AutoUpdate=local' "$HOME/.local/state/woow-quadlet/demo" 2>/dev/null \
    || die_t "run 1 must keep a copy of the hand-edited file it overwrote"
  expect_ok ql_apply_units demo demo.service
  has "$OUT" "demo.service" "run 1 touches the unit"

  : >"$SHIM_STATE/calls"
  changed2=$(ql_install_files "$T/out" demo) || die_t "the second install failed"
  eq "$changed2" '' "run 2 must write no file"
  expect_ok ql_apply_units demo demo.service
  hasnt "$(cat "$SHIM_STATE/calls")" "systemctl --user restart" "run 2 must restart nothing"
  hasnt "$(cat "$SHIM_STATE/calls")" "systemctl --user stop" "run 2 must stop nothing"
  eq "$(cv_drift_of "$CV_QDIR/demo.container")" '' "after the converge the installed file has no drift left"
}

# ---- the downtime report ----------------------------------------------------------------------
t_the_downtime_is_the_gap_between_two_successes() {
  cat >"$T/probe.log" <<'LOG'
1000 200
1100 200
1200 000
1300 000
1400 502
1500 200
1600 200
LOG
  eq "$(cv_probe_downtime_ms "$T/probe.log")" 400 "1100 -> 1500"
}

t_an_uninterrupted_run_reports_zero_and_a_dead_service_reports_minus_one() {
  printf '1000 200\n1100 200\n1200 200\n' >"$T/ok.log"
  eq "$(cv_probe_downtime_ms "$T/ok.log")" 0 "no failed sample means no downtime"
  printf '1000 000\n1100 000\n' >"$T/dead.log"
  eq "$(cv_probe_downtime_ms "$T/dead.log")" -1 "a probe that never succeeded must not round to 0"
  # a password-protected endpoint answers 401 and is still up
  printf '1000 401\n1100 000\n1200 401\n' >"$T/auth.log"
  eq "$(cv_probe_downtime_ms "$T/auth.log")" 200 "401 counts as up"
}

t_the_probe_measures_a_real_restart() {
  # End to end against a real HTTP server on loopback that the test stops and starts again, so
  # the sampler, the curl invocation and the arithmetic are all exercised - not only the
  # arithmetic. This is the same measurement the converge reports as its downtime.
  command -v python3 >/dev/null 2>&1 || { printf 'skip: no python3\n'; return 0; }
  local port=0 srv
  # a free high port: ask the kernel for one, then let it go
  port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  cd "$T"
  python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1 &
  srv=$!
  sleep 0.6
  cv_probe_start "http://127.0.0.1:$port/" "$T/live.log"
  sleep 0.5
  kill "$srv" 2>/dev/null || true; wait "$srv" 2>/dev/null || true
  sleep 0.6
  python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1 &
  srv=$!
  sleep 1.0
  cv_probe_stop
  kill "$srv" 2>/dev/null || true; wait "$srv" 2>/dev/null || true
  d=$(cv_probe_downtime_ms "$T/live.log")
  ((d >= 300 && d <= 2500)) || die_t "measured downtime $d ms is outside the ~600 ms outage the test created (log: $(tr '\n' ' ' <"$T/live.log"))"
}

run() {
  local t=$1 log rc
  [[ -z $FILTER || $t == *"$FILTER"* ]] || return 0
  log=$ROOT/$t.log
  # SC2094: a test may cat its own probe log inside the subshell; the redirection below is the
  # harness's, not that file's.
  # shellcheck disable=SC2094
  (
    set -euo pipefail
    T=$ROOT/$t
    mkdir -p "$T/home/.config/containers/systemd" "$T/home/.config/systemd/user" \
      "$T/state" "$T/run" "$T/bk"
    export HOME=$T/home SHIM_STATE=$T/state XDG_RUNTIME_DIR=$T/run USER=tester TMPDIR=$T
    export PATH="$SHIMS:$PATH" QL_POLL_INTERVAL=0.05 QL_LOG_PREFIX=converge-model
    # The systemd double reads ~/.config/containers/systemd and ~/.config/systemd/user, the
    # real paths, so HOME is what isolates a test - not a QL_QUADLET_DIR override.
    unset QL_DRY_RUN QL_STATE_ROOT QL_CONFIG_ROOT QL_QUADLET_DIR QL_SYSTEMD_USER_DIR
    : >"$SHIM_STATE/calls"
    [[ $(command -v podman) == "$SHIMS/podman" && $(command -v systemctl) == "$SHIMS/systemctl" ]] \
      || die_t "the shims are not first on PATH; refusing to run"
    # shellcheck source=../scripts/lib/quadlet-lib.sh
    . "$REPO/scripts/lib/quadlet-lib.sh"
    # shellcheck source=../scripts/converge-lib.sh
    . "$REPO/scripts/converge-lib.sh"
    CV_STATE=$T/converge.state CV_BACKUP_ROOT=$T/backups
    "$t"
  ) >"$log" 2>&1
  rc=$?
  if ((rc == 0)); then
    npass=$((npass + 1))
    printf 'ok    %s\n' "$t"
  else
    nfail=$((nfail + 1))
    FAILED+=("$t")
    printf 'FAIL  %s\n' "$t"
    tail -n 25 "$log" | sed 's/^/      | /'
  fi
}

for t in $(declare -F | sed -n 's/^declare -f \(t_.*\)$/\1/p'); do run "$t"; done
if [[ -f $HERE/converge-model.local.sh ]]; then
  # shellcheck source=/dev/null
  . "$HERE/converge-model.local.sh"
fi
printf '\n%d passed, %d failed\n' "$npass" "$nfail"
((nfail == 0)) || { printf 'failed: %s\n' "${FAILED[*]}"; exit 1; }
