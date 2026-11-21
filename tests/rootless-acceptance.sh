#!/usr/bin/env bash
# Real destructive acceptance, only on a fresh GitHub-hosted native Ubuntu worker.
# Never run against a pre-existing installation. No LLM credentials or model calls.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
die() { printf 'FAIL rootless acceptance: %s\n' "$*" >&2; exit 1; }
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted && ${RUNNER_OS:-} == Linux ]] \
  || die 'requires a disposable GitHub-hosted Linux worker'
[[ $(id -u) != 0 ]] || die 'requires a non-root user'
arch=${OMNIGENT_ROOTLESS_ARCH:-}
pi_mode=${OMNIGENT_ROOTLESS_PI_STATE:-}
case "$arch:${RUNNER_ARCH:-}:$(uname -m)" in
  amd64:X64:x86_64 | arm64:ARM64:aarch64) ;;
  *) die 'matrix and native host architecture differ' ;;
esac
[[ $pi_mode == private || $pi_mode == shared ]] || die 'invalid Pi fixture mode'
legacy_commit=431759826c6e6ea25579b05051de102699e54229
legacy=${OMNIGENT_ROOTLESS_LEGACY_DIR:-}
assert_checkouts() {
  [[ ${OMNIGENT_ROOTLESS_HEAD:-} =~ ^[0-9a-f]{40}$ ]] || die 'missing reviewed head SHA'
  [[ $(git -C "$REPO" rev-parse HEAD) == "$OMNIGENT_ROOTLESS_HEAD" ]] || die 'candidate checkout differs from reviewed head'
  [[ -d $legacy && $(cd "$legacy" && pwd -P) != "$REPO" ]] || die 'legacy checkout must be separate'
  [[ $(git -C "$legacy" rev-parse HEAD) == "$legacy_commit" ]] || die 'legacy checkout revision differs'
  [[ -z $(git -C "$legacy" status --porcelain --untracked-files=normal) ]] || die 'legacy source was modified'
  [[ $(<"$legacy/VERSION") == 0.21.0-2 && $(<"$REPO/VERSION") == 0.21.0-3 ]] || die 'package version pair differs'
}
assert_checkouts
legacy_head=$(git -C "$legacy" rev-parse HEAD)
[[ -n ${RUNNER_TEMP:-} && -d $RUNNER_TEMP ]] || die 'missing private worker temp directory'
[[ ! -e $HOME/.config/omnigent && ! -e $HOME/.local/state/woow-quadlet/omnigent
  && ! -e $HOME/backups/omnigent ]] || die 'pre-existing installation; refusing to alter it'
if compgen -G "$HOME/.config/containers/systemd/omnigent*" >/dev/null \
  || compgen -G "$HOME/.config/systemd/user/omnigent*" >/dev/null; then
  die 'pre-existing package units'
fi
[[ ! -e $HOME/.config/containers/systemd/pi-agent-data.volume && ! -e $HOME/.config/systemd/user/pi-agent-data-volume.service ]] || die 'pre-existing shared volume unit'
command -v podman >/dev/null || die 'Podman is required'
assert_absent() {
  local kind=$1 name=$2 rc=0
  podman "$kind" exists "$name" >/dev/null 2>&1 || rc=$?
  case $rc in
    1) ;;
    0) die "pre-existing $kind" ;;
    *) die "cannot inspect $kind existence" ;;
  esac
}
for container in omnigent-server omnigent-runner omnigent-postgres omnigent-rootless-peer; do
  assert_absent container "$container"
done
for volume in omnigent-server-data omnigent-pi-data omnigent-postgres-data pi-agent-data; do
  assert_absent volume "$volume"
done
for secret in omnigent-postgres-password omnigent-database-url omnigent-admin-password omnigent-runner-password; do
  assert_absent secret "$secret"
done

umask 077
work=$(mktemp -d "$RUNNER_TEMP/omnigent-rootless.XXXXXX")
phase=preflight
completed=()
rootless_verified=false
report=$RUNNER_TEMP/omnigent-rootless-result.json
export OMNIGENT_ROOTLESS_FAILURE_FILE=$work/failure-code
read_failure_code() {
  local code=unknown
  if [[ -f $OMNIGENT_ROOTLESS_FAILURE_FILE ]]; then
    code=$(<"$OMNIGENT_ROOTLESS_FAILURE_FILE")
  fi
  # Only fixed fixture codes can enter public output, never exception text.
  case $code in
    dispatch|login|create|revoke|secret-hash|cookie|marker|probe|profile-list|state|browser|inventory|storage|peer) ;;
    legacy-arguments|legacy-guard|legacy-state|legacy-schema|legacy-stores|legacy-accounts|legacy-acl|legacy-writers|legacy-mutate) ;;
    *) code=unknown ;;
  esac
  printf '%s' "$code"
}
read_legacy_store_failure() {
  python3 - "$REPO/tests/rootless-legacy-fixtures.py" "$OMNIGENT_ROOTLESS_FAILURE_FILE.json" <<'LEGACY_DIAGNOSTIC'
import json, runpy, sys
helper = runpy.run_path(sys.argv[1])
print(json.dumps(helper['read_store_failure'](sys.argv[2])))
LEGACY_DIAGNOSTIC
}
finish() {
  local rc=$? cleanup_rc=0 cleanup_code='' volume_rc=0 failure_code store_diagnostic=null
  failure_code=$(read_failure_code)
  trap - EXIT
  set +e
  if [[ $failure_code == legacy-stores && -f $OMNIGENT_ROOTLESS_FAILURE_FILE.json ]]; then
    store_diagnostic=$(read_legacy_store_failure 2>/dev/null) || store_diagnostic=null
  fi
  cleanup_failure() { cleanup_rc=1; [[ -n $cleanup_code ]] || cleanup_code=$1; }
  # Only the fresh, named resources this script owns. Never export logs/dumps/secrets.
  systemctl --user stop omnigent-server-health.timer omnigent-runner.service omnigent-server.service omnigent-postgres.service >/dev/null 2>&1
  for container in omnigent-server omnigent-runner omnigent-postgres omnigent-rootless-peer; do
    podman rm --force --ignore "$container" >/dev/null 2>&1 || cleanup_failure cleanup-container
  done
  if [[ $pi_mode == shared ]]; then
    systemctl --user stop pi-agent-data-volume.service >/dev/null 2>&1
    # Podman 4.9 volume rm has no --ignore. All owned containers were removed above;
    # ordinary removal also refuses a volume still held by an unexpected container.
    podman volume exists pi-agent-data >/dev/null 2>&1 || volume_rc=$?
    case $volume_rc in
      0) podman volume rm pi-agent-data >/dev/null 2>&1 || cleanup_failure cleanup-volume ;;
      1) ;; # Nothing was created, or the owned volume was already removed.
      *) cleanup_failure cleanup-volume-inspect ;;
    esac
    rm -f -- "$HOME/.config/containers/systemd/pi-agent-data.volume" || cleanup_failure cleanup-files
  fi
  for volume in omnigent-server-data omnigent-pi-data omnigent-postgres-data; do
    if podman volume exists "$volume"; then podman volume rm "$volume" >/dev/null 2>&1 || cleanup_failure cleanup-volume; fi
  done
  for secret in omnigent-postgres-password omnigent-database-url omnigent-admin-password omnigent-runner-password; do
    if podman secret exists "$secret"; then podman secret rm "$secret" >/dev/null 2>&1 || cleanup_failure cleanup-secret; fi
  done
  rm -f -- "$HOME"/.config/containers/systemd/omnigent* "$HOME"/.config/systemd/user/omnigent* || cleanup_failure cleanup-files
  systemctl --user daemon-reload >/dev/null 2>&1 || cleanup_failure cleanup-systemd
  rm -rf -- "$HOME/.config/omnigent" "$HOME/.local/state/woow-quadlet/omnigent" "$HOME/backups/omnigent" || cleanup_failure cleanup-files
  rm -rf -- "$work" "$REPO/tests/e2e/test-results" "$REPO/tests/e2e/playwright-report" || cleanup_failure cleanup-files
  if ((cleanup_rc != 0)); then
    # Preserve an earlier acceptance failure; otherwise identify cleanup as the failure.
    if ((rc == 0)); then phase=cleanup; failure_code=$cleanup_code; fi
    rc=1
  fi
  OMNIGENT_LEGACY_STORE_FAILURE=$store_diagnostic python3 - "$report" "$REPO" "$rc" "$phase" "$rootless_verified" "$cleanup_rc" "$failure_code" "$arch" "$pi_mode" "$legacy_head" "$OMNIGENT_ROOTLESS_HEAD" "${completed[@]}" <<'PY'
import json, os, pathlib, subprocess, sys
path, repo, rc, phase, rootless, cleanup, failure_code, arch, mode, legacy, expected, *stages = sys.argv[1:]
receipt = json.loads((pathlib.Path(repo) / 'config/server-image.json').read_text())
result = {'status': 'passed' if rc == '0' and phase == 'complete' else 'failed',
          'completedStages': stages, 'lastStage': phase, 'failureCode': failure_code if rc != '0' else None, 'rootlessVerified': rootless == 'true',
          'cleanupComplete': cleanup == '0', 'architecture': 'linux/' + arch, 'piState': mode,
          'legacyPackageCommit': legacy, 'reviewedHead': expected, 'packageVersions': ['0.21.0-2', '0.21.0-3'],
          'sharedCoverage': 'synthetic filesystem interoperability' if mode == 'shared' else None,
          'packageCommit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=repo, text=True).strip(),
          'sourceCommit': receipt['sourceCommit'], 'imageDigest': receipt['imageDigest'],
          'exclusions': ['cold boot', 'full Pi web application', 'paid model execution']}
diagnostic = json.loads(os.environ.get('OMNIGENT_LEGACY_STORE_FAILURE', 'null'))
if rc != '0' and failure_code == 'legacy-stores' and isinstance(diagnostic, dict):
    result['legacyStoreFailure'] = diagnostic
pathlib.Path(path).write_text(json.dumps(result, indent=2)+'\n')
PY
  if ((rc == 0)); then printf 'PASS disposable rootless acceptance\n'; else printf 'FAIL disposable rootless stage %s step %s (exit %s)\n' "$phase" "$failure_code" "$rc"; fi
  exit "$rc"
}
trap finish EXIT

run_stage() {
  phase=$1
  shift
  rm -f -- "$OMNIGENT_ROOTLESS_FAILURE_FILE" "$OMNIGENT_ROOTLESS_FAILURE_FILE.json"
  printf 'RUN %s\n' "$phase"
  local rc=0
  # Keep errexit inside shell functions too; an OR-list would disable it there.
  set +e
  (set -e; "$@") >"$work/$phase.log" 2>&1
  rc=$?
  set -e
  if ((rc)); then
    # Underlying logs may contain credentials. Only fixed unit state fields are public.
    systemctl --user show -p ActiveState -p SubState -p Result omnigent-server.service omnigent-runner.service omnigent-postgres.service 2>/dev/null || true
    return "$rc"
  fi
  completed+=("$phase")
  printf 'PASS %s\n' "$phase"
}
preflight() {
  podman --version | grep -E '^podman version 4\.9\.'
  [[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]]
  [[ $(podman info --format '{{.Host.CgroupsVersion}}') == v2 ]]
  systemctl --user show-environment >/dev/null
  [[ $(loginctl show-user "$(id -un)" -p Linger --value) == yes ]]
}
wait_http() {
  for ((attempt=0; attempt<120; attempt++)); do
    if curl -fsS --max-time 5 http://127.0.0.1:18000/health | jq -e '.status == "ok"' >/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}
legacy_fixture() {
  python3 "$REPO/tests/rootless-legacy-fixtures.py" "$1" "$work/legacy.json" --expected-head "$2" --mcp "$3"
}
assert_writers_stopped() {
  # shellcheck source=../scripts/lib/quadlet-lib.sh
  . "$REPO/scripts/lib/quadlet-lib.sh"
  # shellcheck source=../scripts/writer-state.sh
  . "$REPO/scripts/writer-state.sh"
  omnigent_assert_writers_stopped
}
shared_volume() {
  mkdir -p "$HOME/.config/containers/systemd"
  cp "$REPO/tests/fixtures/refs/pi-agent-data.volume" "$HOME/.config/containers/systemd/pi-agent-data.volume"
  systemctl --user daemon-reload
  systemctl --user start pi-agent-data-volume.service
}
shared_peer() {
  podman run -d --name omnigent-rootless-peer --network=none --userns=keep-id:uid=1000,gid=1000 \
    --entrypoint=/bin/sh -v pi-agent-data:/data/pi-agent localhost/woow-omnigent-runner:0.21.0-2 \
    -c 'while :; do sleep 3600; done'
}
versions() {
  podman exec omnigent-runner pi --version | grep -F '0.85.1'
  podman exec omnigent-runner python3 -c 'import importlib.metadata; assert importlib.metadata.version("omnigent") == "0.21.0"'
}
backup_checkpoint() {
  bash "$REPO/scripts/backup.sh" --stop --include-secrets --dest "$work/$1-backups" >"$work/$1-backup-path"
}
checkpoint_identity() {
  python3 "$REPO/tests/rootless-mcp.py" inventory "$1"
  python3 "$REPO/scripts/backup-checkpoint.py" check --backup "$1" \
    --quadlet-dir "$HOME/.config/containers/systemd" --systemd-dir "$HOME/.config/systemd/user" \
    --env "$HOME/.config/omnigent/omnigent.env"
}
forced_upgrade() {
  local rc=0
  # Keep the flag scoped to this one command. No ordinary failure counts as the
  # deliberate post-migration smoke failure; the private log must contain its marker.
  SMOKE_FORCE_FAIL=1 bash "$REPO/scripts/upgrade.sh" --no-backup >"$work/forced-upgrade.log" 2>&1 || rc=$?
  [[ $rc != 0 ]] || die 'forced upgrade unexpectedly succeeded'
  grep -qF 'FAIL  SMOKE_FORCE_FAIL=1 (forced failure)' "$work/forced-upgrade.log" || die 'upgrade failed before forced smoke'
  legacy_fixture inspect za3b4c5d6e7f present
  assert_writers_stopped
  checkpoint_identity "$legacy_backup"
}

run_stage preflight preflight
rootless_verified=true
run_stage quadlet-dry-run bash "$REPO/tests/dryrun.sh"
if [[ $pi_mode == shared ]]; then run_stage shared-volume shared_volume; fi
# A does not recognize B's optional MCP URL setting. Keep its saved env and units
# consistent until B's installer upgrades them; accounts base supplies loopback URLs.
run_stage install-legacy bash "$legacy/scripts/install.sh" --rebuild --port 18000 --pi-state "$pi_mode" \
  --set OMNIGENT_ACCOUNTS_BASE_URL=http://127.0.0.1:18000
run_stage legacy-versions versions
if [[ $pi_mode == shared ]]; then run_stage shared-peer shared_peer; fi
run_stage legacy-seed legacy_fixture seed d0a1f879a01d absent
run_stage legacy-baseline legacy_fixture verify d0a1f879a01d absent
run_stage legacy-storage python3 "$REPO/tests/rootless-mcp.py" checkpoint-seed "$work/legacy-storage.json"
# Only B's backup script captures the unit/config metadata needed for safe restore.
run_stage legacy-backup backup_checkpoint legacy
legacy_backup=$(<"$work/legacy-backup-path")
[[ -d $legacy_backup && $legacy_backup == "$work/legacy-backups/"* ]] || die 'invalid legacy checkpoint path'
run_stage legacy-checkpoint-identity checkpoint_identity "$legacy_backup"
run_stage legacy-backup-health wait_http
run_stage forced-upgrade forced_upgrade
run_stage legacy-database-mutation legacy_fixture mutate za3b4c5d6e7f present
run_stage legacy-storage-mutation python3 "$REPO/tests/rootless-mcp.py" storage-mutate "$work/legacy-storage.json"
run_stage rollback-maintenance systemctl --user stop omnigent-server-health.timer
run_stage legacy-restore bash "$REPO/scripts/restore.sh" "$legacy_backup" --with-secrets --yes
run_stage legacy-restored legacy_fixture verify d0a1f879a01d absent
run_stage legacy-storage-restored python3 "$REPO/tests/rootless-mcp.py" checkpoint-restored "$work/legacy-storage.json"
run_stage legacy-restored-identity checkpoint_identity "$legacy_backup"
run_stage upgrade-A-to-B bash "$REPO/scripts/upgrade.sh" --no-backup
run_stage upgraded-legacy legacy_fixture verify za3b4c5d6e7f present
run_stage upgraded-storage python3 "$REPO/tests/rootless-mcp.py" checkpoint-restored "$work/legacy-storage.json"
# Now B owns the env/config. Exercise its explicit complete-base URL passthrough.
run_stage candidate-public-base bash "$REPO/scripts/install.sh" --set OMNIGENT_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18000
run_stage runtime-receipt bash "$REPO/tests/smoke-container.sh"
run_stage runner-versions versions
run_stage browser-mcp python3 "$REPO/tests/rootless-mcp.py" browser "$work/profiles.json"
run_stage prepare-profiles python3 "$REPO/tests/rootless-mcp.py" prepare "$work/profiles.json"
run_stage restart systemctl --user restart omnigent-server.service omnigent-runner.service
run_stage restart-health wait_http
run_stage restart-smoke bash "$REPO/tests/smoke.sh"
run_stage restart-retention python3 "$REPO/tests/rootless-mcp.py" check "$work/profiles.json"
run_stage same-candidate-upgrade bash "$REPO/scripts/upgrade.sh" --no-backup
run_stage upgrade-retention python3 "$REPO/tests/rootless-mcp.py" check "$work/profiles.json"
run_stage backup backup_checkpoint candidate
backup_dir=$(<"$work/candidate-backup-path")
[[ -d $backup_dir && $backup_dir == "$work/candidate-backups/"* ]] || die 'invalid checkpoint path'
run_stage candidate-checkpoint-identity checkpoint_identity "$backup_dir"
# Backup resumes systemd units before the HTTP server has finished starting.
run_stage backup-resume-health wait_http
run_stage mutate-after-checkpoint python3 "$REPO/tests/rootless-mcp.py" mutate "$work/profiles.json"
run_stage maintenance systemctl --user stop omnigent-server-health.timer omnigent-runner.service omnigent-server.service
run_stage candidate-writers-stopped assert_writers_stopped
run_stage candidate-database-mutation legacy_fixture mutate za3b4c5d6e7f present
run_stage candidate-storage-mutation python3 "$REPO/tests/rootless-mcp.py" storage-mutate "$work/profiles.json"
run_stage restore bash "$REPO/scripts/restore.sh" "$backup_dir" --with-secrets --yes
run_stage restored-state python3 "$REPO/tests/rootless-mcp.py" restored "$work/profiles.json"
run_stage restored-legacy legacy_fixture verify za3b4c5d6e7f present
run_stage restored-runtime bash "$REPO/tests/smoke-container.sh"
run_stage source-identity assert_checkouts
phase=complete
