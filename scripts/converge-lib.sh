# shellcheck shell=bash
# shellcheck disable=SC2034 # these settings are read by the script that sources this file
# scripts/converge-lib.sh: the operator's safety net around scripts/install.sh for a host whose
# Quadlet units were installed and then edited BY HAND (woowtechopenclaw). Sourced after
# scripts/lib/quadlet-lib.sh, never executed.
#
# This is not a migration. The stack is already Quadlet, the container names, the volumes and
# the network are already the ones the repo declares, and install.sh already adopts a foreign
# unit file with our name after taking a backup copy and restarts only what changed. Converging
# therefore IS `scripts/install.sh` - the same path Woow_podman_pi_agent_package took on
# toypark1234, where repointing a hand-edited pi-web.container at %h/%t cost 1.5 s of downtime.
#
# What install.sh on its own cannot give an operator is the evidence around that one command:
# a pre-flight that refuses instead of guessing, a checksummed backup taken before anything is
# overwritten, a readable report of WHAT drifted, proof that the data was adopted rather than
# re-created, a measured downtime, and one command that puts the previous units back. Those are
# the helpers below; none of them starts, stops or edits a service by itself.
#
# The caller sets: CV_APP, CV_QDIR, CV_STATE_DIR, CV_BACKUP_ROOT before using them.

CV_QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
CV_SYSTEMD_USER_DIR=${QL_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}

cv_running() { [[ $(podman inspect --format '{{.State.Running}}' "$1" 2>/dev/null) == true ]]; }
cv_unit_exists() { [[ -n $(systemctl --user show -p FragmentPath --value "$1" 2>/dev/null) ]]; }
# cv_now_ms: milliseconds since the epoch, for the downtime report
cv_now_ms() { date +%s%3N; }

# cv_state_get / cv_state_set <key> [value]: the converge's own record, next to the lib's state
cv_state_get() { if [[ -f $CV_STATE ]]; then sed -n "s/^$1=//p" "$CV_STATE" | tail -n1; fi; }
cv_state_set() {
  local tmp
  mkdir -p "${CV_STATE%/*}"
  tmp=$(mktemp "${CV_STATE%/*}/.converge.XXXXXX")
  { if [[ -f $CV_STATE ]]; then grep -v "^$1=" "$CV_STATE" || true; fi; printf '%s=%s\n' "$1" "$2"; } >"$tmp"
  mv -f "$tmp" "$CV_STATE"
}

# cv_new_backup_dir [dir]: a fresh private directory (0700)
cv_new_backup_dir() {
  local d=${1:-} base i=2
  if [[ -z $d ]]; then
    base=$CV_BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)
    d=$base
    while [[ -e $d ]]; do d=$base-$i; i=$((i + 1)); done
  fi
  [[ ! -e $d ]] || ql_die "$d already exists"
  (umask 077 && mkdir -p -- "$d") || ql_die "cannot create $d"
  printf '%s' "$d"
}

# cv_write_checksums <dir>: SHA256SUMS in `sha256sum -c` format over every file in the backup
cv_write_checksums() {
  local d=${1:?usage: cv_write_checksums <dir>} list
  list=$(cd -- "$d" && find . -type f ! -name 'SHA256SUMS*' ! -name '*.sha256' -printf '%P\n' | LC_ALL=C sort)
  [[ -n $list ]] || return 0
  (cd -- "$d" && printf '%s\n' "$list" | tr '\n' '\0' | xargs -0 sha256sum >SHA256SUMS.partial) \
    || { rm -f -- "$d/SHA256SUMS.partial"; ql_die "cannot checksum $d"; }
  mv -f -- "$d/SHA256SUMS.partial" "$d/SHA256SUMS"
  chmod 600 -- "$d/SHA256SUMS"
}

# cv_volume_identity <volume>: "<CreatedAt> <mountpoint inode>". A Quadlet .volume adopts an
# existing volume BY NAME; that is only worth trusting if the name still resolves to the same
# directory. A volume that was removed and made again has a new CreatedAt and a new inode, so
# comparing this string across the converge is what turns "adopted" from a claim into a check.
cv_volume_identity() {
  local v=${1:?usage: cv_volume_identity <volume>} mp created ino
  mp=$(podman volume inspect --format '{{.Mountpoint}}' "$v" 2>/dev/null) || return 1
  created=$(podman volume inspect --format '{{.CreatedAt}}' "$v" 2>/dev/null) || return 1
  [[ -n $mp ]] || return 1
  ino=$(podman unshare stat -c '%i' -- "$mp" 2>/dev/null) || ino='?'
  printf '%s %s' "$created" "$ino"
}

# cv_drift_of <installed file>: the hand-edit markers this package converges away, one per
# line, or nothing when the file is already in the repo's shape. Reading the file rather than
# diffing it means the report says WHY a file changes, which is what an operator needs before
# approving a restart of a live service.
#
#   literal-home      /home/<user> where the repo writes %h   (breaks on a different account)
#   literal-runtime   /run/user/<uid> where the repo writes %t
#   floating-tag      Image= with :latest/:stable/no tag      (STANDARD section 3)
#   autoupdate        AutoUpdate=local, i.e. an out-of-band image switch under systemd
#   plaintext-secret  a password/secret/token/key spelled into the unit (STANDARD section 4)
#   no-success-exit   no SuccessExitStatus=143: a clean stop leaves the unit failed
#   plain-volume-ref  Volume=<name>:... instead of a .volume unit, so there is no ordering
cv_drift_of() {
  local f=${1:?usage: cv_drift_of <file>} body
  [[ -f $f ]] || return 0
  body=$(grep -vE '^[[:space:]]*#' -- "$f")
  grep -qE '(^|[=:[:space:]])/home/[A-Za-z0-9._-]+' <<<"$body" && printf 'literal-home\n'
  grep -qE '(^|[=:[:space:]])/run/user/[0-9]+' <<<"$body" && printf 'literal-runtime\n'
  grep -qE '^Image=.*:(latest|stable)$' <<<"$body" && printf 'floating-tag\n'
  grep -qE '^Image=[^:]*$' <<<"$body" && printf 'floating-tag\n'
  grep -qE '^AutoUpdate=' <<<"$body" && printf 'autoupdate\n'
  grep -qiE '^Environment=[A-Za-z0-9_]*(PASSWORD|SECRET|TOKEN|_KEY)=.+' <<<"$body" && printf 'plaintext-secret\n'
  grep -qiE '^Environment=[A-Za-z0-9_]*URL=[a-z+]+://[^:@[:space:]]+:[^@[:space:]]+@' <<<"$body" && printf 'plaintext-secret\n'
  if grep -q '^\[Container\]' <<<"$body" && ! grep -q '^SuccessExitStatus=143' <<<"$body"; then
    printf 'no-success-exit\n'
  fi
  # A bare volume NAME, not a `.volume` unit reference and not a host path: podman then has no
  # systemd dependency on the volume at all. `x.volume:/data` and `%h/x:/data` are correct.
  if grep -E '^Volume=[A-Za-z0-9][A-Za-z0-9_.-]*:/' <<<"$body" | grep -qv '^Volume=[^:]*\.volume:'; then
    printf 'plain-volume-ref\n'
  fi
  return 0
}

# cv_drift_report <file...>: prints one line per installed file and returns 1 when anything
# drifted. A file that is not installed at all is reported as "absent" (install.sh will write
# it), a file byte-identical to what install.sh renders is reported as "clean".
cv_drift_report() {
  local f base marks n=0
  for f in "$@"; do
    base=${f##*/}
    if [[ ! -f $CV_QDIR/$base && ! -f $CV_SYSTEMD_USER_DIR/$base ]]; then
      printf '  %-42s absent (install.sh will write it)\n' "$base"
      n=$((n + 1))
      continue
    fi
    [[ -f $CV_QDIR/$base ]] && f=$CV_QDIR/$base || f=$CV_SYSTEMD_USER_DIR/$base
    marks=$(cv_drift_of "$f" | LC_ALL=C sort -u | tr '\n' ',')
    marks=${marks%,}
    if [[ -z $marks ]]; then
      printf '  %-42s clean\n' "$base"
    else
      printf '  %-42s %s\n' "$base" "$marks"
      n=$((n + 1))
    fi
  done
  ((n == 0))
}

# cv_backup_units <backup dir> <file...>: a copy of every installed unit file this package owns,
# before install.sh overwrites any of them. This is what --rollback puts back.
cv_backup_units() {
  local bk=${1:?usage: cv_backup_units <dir> <file...>} f base src
  shift
  mkdir -p "$bk/units"
  for f in "$@"; do
    base=${f##*/}
    src=''
    [[ -f $CV_QDIR/$base ]] && src=$CV_QDIR/$base
    [[ -z $src && -f $CV_SYSTEMD_USER_DIR/$base ]] && src=$CV_SYSTEMD_USER_DIR/$base
    [[ -n $src ]] || continue
    cp -p -- "$src" "$bk/units/$base"
    printf '%s\n' "$src" >>"$bk/units/.paths"
  done
  chmod -R go-rwx -- "$bk/units"
}

# cv_restore_units <backup dir>: put the saved unit files back where they came from. The images
# the old units named are still on the host (nothing in the converge removes an image), so a
# daemon-reload plus a restart of the affected units brings the previous stack back.
cv_restore_units() {
  local bk=${1:?usage: cv_restore_units <dir>} p base
  [[ -f $bk/units/.paths ]] || ql_die "no saved unit files in $bk/units"
  while IFS= read -r p; do
    base=${p##*/}
    [[ -f $bk/units/$base ]] || continue
    install -m 644 -- "$bk/units/$base" "$p" || ql_die "cannot restore $p"
    ql_info "restored $p"
  done <"$bk/units/.paths"
}

# ---- downtime probe -------------------------------------------------------------------------
# The converge restarts a live service. "How long was it down" is not answerable from
# .State.StartedAt (that is when the NEW container started, not when the old one stopped), so a
# background prober samples the health URL and the gap between the last success before the
# restart and the first success after it is the downtime.
#
# cv_probe_start <url> <logfile>; cv_probe_stop; cv_probe_downtime_ms <logfile>
cv_probe_start() {
  local url=${1:?usage: cv_probe_start <url> <log>} log=${2:?}
  : >"$log"
  (
    while :; do
      printf '%s %s\n' "$(date +%s%3N)" \
        "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$url" 2>/dev/null || true)" >>"$log"
      sleep 0.1
    done
  ) &
  CV_PROBE_PID=$!
}
cv_probe_stop() {
  [[ -n ${CV_PROBE_PID:-} ]] || return 0
  kill "$CV_PROBE_PID" 2>/dev/null || true
  wait "$CV_PROBE_PID" 2>/dev/null || true
  CV_PROBE_PID=''
}
# cv_probe_downtime_ms <log>: the longest gap between two successful samples, in milliseconds,
# counting only gaps that contain at least one failed sample. A run that never failed prints 0;
# a probe that never succeeded prints -1, which the caller must report rather than round to 0.
cv_probe_downtime_ms() {
  local log=${1:?usage: cv_probe_downtime_ms <log>}
  awk '
    $2 ~ /^[23]/ || $2 == "401" || $2 == "403" {
      if (lastok != "" && failed) { d = $1 - lastok; if (d > max) max = d }
      lastok = $1; failed = 0; seen = 1; next
    }
    { failed = 1 }
    END { if (!seen) { print -1 } else { print max + 0 } }
  ' "$log"
}

# cv_adopt_plain_units <app> <rendered dir> <doc url> <unit...>
# Take over the plain helper units that a hand install copied straight into
# ~/.config/systemd/user. They are ours - same Documentation= URL - but in no manifest, so
# ql_install_files refuses them as foreign units it must not overwrite. Each one that is still
# un-manifested and differs from the unit we are about to install is moved aside by
# ql_adopt_file (a copy is kept under <state>/<app>/adopted/), which under QL_DRY_RUN=1 records
# the move instead of doing it: that is what lets a dry run on a host that has not been
# converged yet report what the real run would do, rather than die in ql_install_files on a
# collision the real run never reaches. A file with the same bytes is left alone, and a file
# that is not ours is fatal - the same refusal as before.
#
# Same shape as Woow_podman_pi_agent_package's pi_adopt_legacy_units, which is the converge
# that ran on toypark1234.
cv_adopt_plain_units() {
  local app=$1 out=$2 doc=$3
  shift 3
  local u p sdir manifest
  sdir=$CV_SYSTEMD_USER_DIR
  manifest=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$app/manifest
  for u in "$@"; do
    p=$sdir/$u
    [[ -f $p && ! -L $p ]] || continue
    if [[ -f $manifest ]] && grep -qF "  $p" "$manifest"; then continue; fi
    cmp -s "$p" "$out/$u" && continue
    grep -qxF "$doc" "$p" || ql_die "$p exists and was not installed by this package; move it away first"
    ql_adopt_file "$app" "$p"
  done
  return 0
}
