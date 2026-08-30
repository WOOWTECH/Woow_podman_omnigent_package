#!/usr/bin/env bash
# Smoke: runner can reach omnigent-server over the private omnigent network.
# Does NOT assert the runner has registered as an active worker — that
# subcommand form is being iterated on live; see rootfs/usr/local/bin/
# omnigent-runner-loop for the current state.
set -uo pipefail

PASS_N=0; FAIL_N=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS_N=$((PASS_N+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL_N=$((FAIL_N+1)); }

echo "== Runner sees omnigent-server on the private podman network =="
CODE="$(podman exec omnigent-runner curl -sS -o /dev/null -w '%{http_code}' \
        --max-time 5 http://omnigent-server:8000/health 2>&1)"
[ "${CODE}" = "200" ] && ok "omnigent-server:8000/health -> ${CODE} from inside runner" \
                       || bad "omnigent-server:8000/health -> ${CODE} from inside runner"

echo
echo "== omnigent CLI is on the runner PATH =="
V="$(podman exec omnigent-runner omnigent --version 2>&1 || true)"
if echo "${V}" | grep -qE 'omnigent[[:space:]]*[0-9]'; then
    ok "omnigent --version = ${V}"
else
    bad "omnigent not on PATH or version parse failed: ${V}"
fi

echo
printf '  %d passed, %d failed\n\n' "${PASS_N}" "${FAIL_N}"
[ "${FAIL_N}" -eq 0 ] || exit 1
