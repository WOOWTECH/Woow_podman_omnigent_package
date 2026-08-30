#!/usr/bin/env bash
# Smoke: runner has pi 0.83.0, pi-code wrapper installed, /data/pi-agent
# mounted with sibling artefacts visible, OMNIGENT_PI_PATH set right.
set -uo pipefail

EXPECTED_PI_VERSION="${EXPECTED_PI_VERSION:-0.83.0}"
PASS_N=0; FAIL_N=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS_N=$((PASS_N+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL_N=$((FAIL_N+1)); }
skip(){ printf '  \033[90mSKIP\033[0m  %s\n' "$*"; }

CX() { podman exec omnigent-runner "$@"; }

echo "== Runner has pi CLI + wrapper =="
if V="$(CX pi --version 2>&1)"; then
    [ "${V}" = "${EXPECTED_PI_VERSION}" ] \
        && ok "pi --version = ${V}" \
        || bad "pi --version = ${V} (expected ${EXPECTED_PI_VERSION})"
else
    bad "pi not on PATH inside runner"
fi

if CX test -x /usr/local/bin/pi-code; then
    ok "pi-code wrapper installed + executable"
else
    bad "pi-code wrapper missing"
fi

echo
echo "== Shared /data/pi-agent volume =="
if CX test -d /data/pi-agent; then
    ok "/data/pi-agent mounted"
    for f in models-store.json home sessions; do
        if CX test -e "/data/pi-agent/${f}"; then
            ok "  sees sibling artefact /data/pi-agent/${f}"
        else
            skip "  /data/pi-agent/${f} absent (sibling pi-web not deployed?)"
        fi
    done
else
    bad "/data/pi-agent NOT mounted — pi state won't persist / share"
fi

echo
echo "== OMNIGENT_PI_PATH steers spawns to pi-code =="
V="$(CX printenv OMNIGENT_PI_PATH 2>&1 || true)"
[ "${V}" = "/usr/local/bin/pi-code" ] \
    && ok "OMNIGENT_PI_PATH=${V}" \
    || bad "OMNIGENT_PI_PATH=${V} (expected /usr/local/bin/pi-code)"

echo
printf '  %d passed, %d failed\n\n' "${PASS_N}" "${FAIL_N}"
[ "${FAIL_N}" -eq 0 ] || exit 1
