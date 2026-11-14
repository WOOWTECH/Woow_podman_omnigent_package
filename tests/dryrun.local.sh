# shellcheck shell=bash
# tests/dryrun.local.sh: repo-specific checks, sourced at the end of tests/dryrun.sh (which
# defines REPO, WORK, APP, failures and run_variant). CI runs it through tests/dryrun.sh.
# shellcheck disable=SC2154 # REPO and failures come from tests/dryrun.sh

local_fail() { echo "FAIL $*"; failures=$((failures + 1)); }

# One version, four places: VERSION (<omnigent version>-<package revision>), the runner's
# Image= tag, ARG OMNIGENT_VERSION in Containerfile.runner, and the server's Image= tag.
ver=$(<"$REPO/VERSION")
runner_tag=$(sed -n 's|^Image=localhost/woow-omnigent-runner:||p' "$REPO/quadlet/omnigent-runner.container")
arg_ver=$(sed -n 's/^ARG OMNIGENT_VERSION=//p' "$REPO/Containerfile.runner")
server_ref=$(sed -n 's|^Image=ghcr.io/omnigent-ai/omnigent-server:v||p' "$REPO/quadlet/omnigent-server.container")
server_tag=${server_ref%%@*}
if [[ $ver == "$runner_tag" && ${ver%%-*} == "$arg_ver" && ${ver%%-*} == "$server_tag" ]]; then
  echo "ok   versions agree (VERSION=$ver, omnigent $arg_ver)"
else
  local_fail "versions disagree: VERSION=$ver runner=$runner_tag ARG=$arg_ver server=v$server_tag"
fi

# The server and Postgres images are pinned by digest as well as by tag (tag@sha256:<64 hex>),
# so a dropped digest cannot slip through a version bump.
for unit in server postgres; do
  img=$(sed -n 's|^Image=||p' "$REPO/quadlet/omnigent-$unit.container")
  if [[ $img =~ ^[^@]+:[^@:/]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "ok   omnigent-$unit image pinned by tag and digest"
  else
    local_fail "omnigent-$unit.container Image=$img is not tag@sha256:<digest>"
  fi
done

# Account isolation and first-boot security must survive later edits to the units.
server_unit=$REPO/quadlet/omnigent-server.container
runner_unit=$REPO/quadlet/omnigent-runner.container
if grep -qF 'Secret=omnigent-admin-password,type=env,target=OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD' "$server_unit" \
  && grep -qF 'Secret=omnigent-runner-password,type=env,target=OMNIGENT_RUNNER_PASSWORD' "$runner_unit" \
  && ! grep -qE 'omnigent-admin-password|OMNIGENT_ADMIN_PASSWORD|OMNIGENT_ADMIN_USERNAME' "$runner_unit" \
  && grep -qF 'Environment=OMNIGENT_PUBLIC_SHARING=0' "$server_unit" \
  && grep -qF 'DropCapability=ALL' "$server_unit" \
  && grep -qF 'DropCapability=ALL' "$runner_unit"; then
  echo 'ok   admin is pre-seeded on the server; the runner holds only its operator credential'
else
  local_fail 'admin pre-seeding or runner credential isolation changed'
fi

# Settings > Sharing writes its overrides into the directory of OMNIGENT_ADMIN_CREDENTIALS_PATH.
# Unless that is on the server volume, a restart silently puts every setting back.
if grep -qx 'Environment=OMNIGENT_ADMIN_CREDENTIALS_PATH=/data/admin-credentials' "$server_unit" \
  && grep -qx 'Volume=omnigent-server.volume:/data' "$server_unit" \
  && grep -qx 'Environment=OMNIGENT_GRANT_MAX_LIFETIME_DAYS=365' "$server_unit"; then
  echo 'ok   admin settings are kept on the server volume; grants last 365 days as on PaaS'
else
  local_fail 'the server data dir is not on omnigent-server-data, or grants do not last 365 days'
fi

# The runner base image must be pinned to an exact version (STANDARD section 3).
if grep -E '^FROM ' "$REPO/Containerfile.runner" | grep -qvE ':[0-9]+\.[0-9]+\.[0-9]+-'; then
  local_fail "Containerfile.runner has a FROM line without an exact version"
else
  echo "ok   Containerfile.runner base image pinned"
fi

# Loopback and private pi state by default.
if grep -qx 'OMNIGENT_BIND=127.0.0.1' "$REPO/config/omnigent.env.example" \
  && grep -qx 'OMNIGENT_PI_STATE=private' "$REPO/config/omnigent.env.example"; then
  echo "ok   example binds 127.0.0.1 with private pi state"
else
  local_fail "config/omnigent.env.example must default to OMNIGENT_BIND=127.0.0.1 and OMNIGENT_PI_STATE=private"
fi

# The credentials this repo used to commit must never come back. The patterns are written
# with a bracketed letter so this file does not match itself.
# A password that starts with $ or < is a shell variable or a <placeholder>, not a secret.
creds='woowtech20[2]6|PASSWORD=woowtec[h]|://[^:/@[:space:]]+:[^$<@/[:space:]][^@/[:space:]]{3,}@|(PASSWORD|DATABASE_URL|SECRET|TOKEN)=[^$@"<[:space:]]{6,}'
if grep -rnIE --exclude-dir=node_modules --exclude-dir=lib --exclude=dryrun.local.sh "$creds" \
  "$REPO/quadlet" "$REPO/config" "$REPO/systemd" "$REPO/scripts" "$REPO/tests" "$REPO/rootfs" \
  "$REPO/Containerfile.runner" "$REPO/README.md" "$REPO/README_zh-TW.md"; then
  local_fail "something credential-shaped is committed (see the lines above)"
else
  echo "ok   no credential-shaped strings in the units, scripts, tests or READMEs"
fi

# ------------------------------------------------------------------------------------------
# Regression: no check in tests/smoke.sh may put a killable producer in a pipeline.
#
# `podman logs omnigent-runner 2>&1 | grep -qF -- "$1"` looks right and is not: grep -q exits
# at the first match, podman logs then takes SIGPIPE and exits 141, and `set -o pipefail`
# makes PIPESTATUS[0] the pipeline's status. Both runner checks therefore FAILED on every
# healthy install (grep -c found each string), and scripts/install.sh exited 1 after its
# 180 s wait. Nothing caught it because nothing ever executed runner_log_has.
#
# This runs the real function out of tests/smoke.sh against a stub `podman` whose log is
# larger than the 64 KiB pipe buffer -- the size at which the producer is actually killed.
smoke_fn() { # smoke_fn <name>: the source of that function, as tests/smoke.sh defines it
  local line found=0 depth=0 open close
  while IFS= read -r line; do
    if ((!found)); then
      [[ $line == "$1()"*'{'* ]] || continue
      found=1
    fi
    printf '%s\n' "$line"
    open=${line//[^\{]/} close=${line//[^\}]/}
    depth=$((depth + ${#open} - ${#close}))
    ((depth > 0)) || break
  done <"$REPO/tests/smoke.sh"
  ((found))
}

pipe_stub=$WORK/sigpipe-stub
mkdir -p "$pipe_stub"
cat >"$pipe_stub/podman" <<'STUB'
#!/usr/bin/env bash
# stand-in for `podman logs omnigent-runner`: the markers the smoke test looks for, then
# well over 64 KiB of further output, so a consumer that exits early leaves this writing.
[[ ${1:-} == logs ]] || exit 0
printf 'omnigent-runner: logged in as admin (access token 28800 s, refresh grant stored)\n'
printf 'omnigent-runner: tailing /data/pi-agent/logs/host.log\n'
for i in $(seq 1 4000); do printf 'omnigent-runner: line %s polling for work\n' "$i"; done
STUB
chmod 755 "$pipe_stub/podman"

if fn=$(smoke_fn runner_log_has); then
  rc=0
  PATH=$pipe_stub:$PATH bash -c "set -uo pipefail
$fn
runner_log_has 'tailing'" || rc=$?
  if ((rc == 0)); then
    echo "ok   smoke.sh runner_log_has finds a marker in a log bigger than the pipe buffer"
  else
    local_fail "smoke.sh runner_log_has returned $rc for a string that IS in the log (141 = the producer was killed by SIGPIPE and pipefail failed the check)"
  fi
else
  local_fail "tests/smoke.sh no longer defines runner_log_has(); this regression test cannot run"
fi
unset -f smoke_fn
