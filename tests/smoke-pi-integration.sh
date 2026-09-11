#!/usr/bin/env bash
# Smoke: the runner has the pinned pi, the pi-code wrapper is installed, /data/pi-agent is
# mounted and OMNIGENT_PI_PATH points at the wrapper. The sibling-artefact checks only mean
# something in OMNIGENT_PI_STATE=shared mode; in private mode they SKIP until someone has
# run `podman exec -it omnigent-runner pi login`.
set -uo pipefail

EXPECTED_PI_VERSION="${EXPECTED_PI_VERSION:-0.85.1}"
PASS_N=0; FAIL_N=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS_N=$((PASS_N+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL_N=$((FAIL_N+1)); }
skip(){ printf '  \033[90mSKIP\033[0m  %s\n' "$*"; }

CX() { podman exec omnigent-runner "$@"; }

echo "== Runner has pi CLI + wrapper =="
if V="$(CX pi --version 2>&1)"; then
    if [ "${V}" = "${EXPECTED_PI_VERSION}" ]; then
        ok "pi --version = ${V}"
    else
        bad "pi --version = ${V} (expected ${EXPECTED_PI_VERSION})"
    fi
else
    bad "pi not on PATH inside runner"
fi

if CX test -x /usr/local/bin/pi-code; then
    ok "pi-code wrapper installed + executable"
else
    bad "pi-code wrapper missing"
fi

echo
echo "== /data/pi-agent volume =="
if CX test -d /data/pi-agent; then
    ok "/data/pi-agent mounted"
    for f in models-store.json home sessions; do
        if CX test -e "/data/pi-agent/${f}"; then
            ok "  sees sibling artefact /data/pi-agent/${f}"
        else
            skip "  /data/pi-agent/${f} absent (private pi state, or no pi login yet)"
        fi
    done
else
    bad "/data/pi-agent NOT mounted — pi state will not persist"
fi

echo
echo "== OMNIGENT_PI_PATH steers spawns to pi-code =="
V="$(CX printenv OMNIGENT_PI_PATH 2>&1 || true)"
if [ "${V}" = "/usr/local/bin/pi-code" ]; then
    ok "OMNIGENT_PI_PATH=${V}"
else
    bad "OMNIGENT_PI_PATH=${V} (expected /usr/local/bin/pi-code)"
fi

echo
printf '  %d passed, %d failed\n\n' "${PASS_N}" "${FAIL_N}"
[ "${FAIL_N}" -eq 0 ] || exit 1
