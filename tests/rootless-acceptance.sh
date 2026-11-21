#!/usr/bin/env bash
# Real destructive acceptance, only on a fresh GitHub-hosted amd64 Ubuntu worker.
# Never run against a pre-existing installation. No LLM credentials or model calls.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
die() { printf 'FAIL rootless acceptance: %s\n' "$*" >&2; exit 1; }
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted \
  && ${RUNNER_OS:-} == Linux && ${RUNNER_ARCH:-} == X64 ]] \
  || die 'requires a disposable GitHub-hosted Linux X64 worker'
[[ $(id -u) != 0 && $(uname -m) == x86_64 ]] || die 'requires an amd64 non-root user'
[[ -n ${RUNNER_TEMP:-} && -d $RUNNER_TEMP ]] || die 'missing private worker temp directory'
[[ ! -e $HOME/.config/omnigent && ! -e $HOME/.local/state/woow-quadlet/omnigent
  && ! -e $HOME/backups/omnigent ]] || die 'pre-existing installation; refusing to alter it'
if compgen -G "$HOME/.config/containers/systemd/omnigent*" >/dev/null \
  || compgen -G "$HOME/.config/systemd/user/omnigent*" >/dev/null; then
  die 'pre-existing package units'
fi
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
for container in omnigent-server omnigent-runner omnigent-postgres; do
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
    dispatch|login|create|revoke|secret-hash|cookie|marker|probe|profile-list|state|browser) ;;
    *) code=unknown ;;
  esac
  printf '%s' "$code"
}
finish() {
  local rc=$? cleanup_rc=0 failure_code
  failure_code=$(read_failure_code)
  trap - EXIT
  set +e
  # Only the fresh, named resources this script owns. Never export logs/dumps/secrets.
  systemctl --user stop omnigent-server-health.timer omnigent-runner.service omnigent-server.service omnigent-postgres.service >/dev/null 2>&1
  for container in omnigent-server omnigent-runner omnigent-postgres; do
    podman rm --force --ignore "$container" >/dev/null 2>&1 || cleanup_rc=1
  done
  for volume in omnigent-server-data omnigent-pi-data omnigent-postgres-data; do
    if podman volume exists "$volume"; then podman volume rm "$volume" >/dev/null 2>&1 || cleanup_rc=1; fi
  done
  for secret in omnigent-postgres-password omnigent-database-url omnigent-admin-password omnigent-runner-password; do
    if podman secret exists "$secret"; then podman secret rm "$secret" >/dev/null 2>&1 || cleanup_rc=1; fi
  done
  rm -f -- "$HOME"/.config/containers/systemd/omnigent* "$HOME"/.config/systemd/user/omnigent*
  systemctl --user daemon-reload >/dev/null 2>&1
  rm -rf -- "$HOME/.config/omnigent" "$HOME/.local/state/woow-quadlet/omnigent" "$HOME/backups/omnigent"
  rm -rf -- "$work" "$REPO/tests/e2e/test-results" "$REPO/tests/e2e/playwright-report"
  ((cleanup_rc == 0)) || rc=1
  python3 - "$report" "$REPO" "$rc" "$phase" "$rootless_verified" "$cleanup_rc" "$failure_code" "${completed[@]}" <<'PY'
import json, pathlib, subprocess, sys
path, repo, rc, phase, rootless, cleanup, failure_code, *stages = sys.argv[1:]
receipt = json.loads((pathlib.Path(repo) / 'config/server-image.json').read_text())
result = {'status': 'passed' if rc == '0' and phase == 'complete' else 'failed',
          'completedStages': stages, 'lastStage': phase, 'failureCode': failure_code if rc != '0' else None, 'rootlessVerified': rootless == 'true',
          'cleanupComplete': cleanup == '0', 'architecture': 'linux/amd64', 'piState': 'private',
          'packageCommit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=repo, text=True).strip(),
          'sourceCommit': receipt['sourceCommit'], 'imageDigest': receipt['imageDigest'],
          'exclusions': ['cold boot', 'arm64 rootless', 'shared Pi', 'paid model execution', 'A-to-B schema migration']}
pathlib.Path(path).write_text(json.dumps(result, indent=2)+'\n')
PY
  if ((rc == 0)); then printf 'PASS disposable rootless acceptance\n'; else printf 'FAIL disposable rootless stage %s step %s (exit %s)\n' "$phase" "$failure_code" "$rc"; fi
  exit "$rc"
}
trap finish EXIT

run_stage() {
  phase=$1
  shift
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
run_stage preflight preflight
rootless_verified=true
run_stage quadlet-dry-run bash "$REPO/tests/dryrun.sh"
run_stage install bash "$REPO/scripts/install.sh" --port 18000 --pi-state private \
  --set OMNIGENT_ACCOUNTS_BASE_URL=http://127.0.0.1:18000 \
  --set OMNIGENT_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18000
run_stage runtime-receipt bash "$REPO/tests/smoke-container.sh"
versions() {
  podman exec omnigent-runner pi --version | grep -F '0.85.1'
  podman exec omnigent-runner python3 -c 'import importlib.metadata; assert importlib.metadata.version("omnigent") == "0.21.0"'
}
run_stage runner-versions versions
run_stage browser-mcp python3 "$REPO/tests/rootless-mcp.py" browser "$work/profiles.json"
run_stage prepare-profiles python3 "$REPO/tests/rootless-mcp.py" prepare "$work/profiles.json"
run_stage restart systemctl --user restart omnigent-server.service omnigent-runner.service
wait_http() {
  for ((attempt=0; attempt<120; attempt++)); do
    if curl -fsS --max-time 5 http://127.0.0.1:18000/health | jq -e '.status == "ok"' >/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}
run_stage restart-health wait_http
run_stage restart-smoke bash "$REPO/tests/smoke.sh"
run_stage restart-retention python3 "$REPO/tests/rootless-mcp.py" check "$work/profiles.json"
run_stage same-candidate-upgrade bash "$REPO/scripts/upgrade.sh"
run_stage upgrade-retention python3 "$REPO/tests/rootless-mcp.py" check "$work/profiles.json"
backup_checkpoint() {
  bash "$REPO/scripts/backup.sh" --stop --include-secrets --dest "$work/backups" >"$work/backup-path"
}
run_stage backup backup_checkpoint
backup_dir=$(<"$work/backup-path")
[[ -d $backup_dir && $backup_dir == "$work/backups/"* ]] || die 'invalid checkpoint path'
# Backup resumes systemd units before the HTTP server has finished starting.
run_stage backup-resume-health wait_http
run_stage mutate-after-checkpoint python3 "$REPO/tests/rootless-mcp.py" mutate "$work/profiles.json"
run_stage maintenance systemctl --user stop omnigent-server-health.timer
run_stage restore bash "$REPO/scripts/restore.sh" "$backup_dir" --with-secrets --yes
run_stage restored-state python3 "$REPO/tests/rootless-mcp.py" restored "$work/profiles.json"
run_stage restored-runtime bash "$REPO/tests/smoke-container.sh"
phase=complete
