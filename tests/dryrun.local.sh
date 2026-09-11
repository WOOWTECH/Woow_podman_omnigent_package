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
server_tag=$(sed -n 's|^Image=ghcr.io/omnigent-ai/omnigent-server:v||p' "$REPO/quadlet/omnigent-server.container")
if [[ $ver == "$runner_tag" && ${ver%%-*} == "$arg_ver" && ${ver%%-*} == "$server_tag" ]]; then
  echo "ok   versions agree (VERSION=$ver, omnigent $arg_ver)"
else
  local_fail "versions disagree: VERSION=$ver runner=$runner_tag ARG=$arg_ver server=v$server_tag"
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
