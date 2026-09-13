#!/usr/bin/env bash
# Smoke: 3 containers up, server /health 200 JSON, postgres pg_isready green.
# NB: /healthz is shadowed by the React SPA catch-all and returns HTML 200
# even when the API is dead — use /health instead ({"status":"ok"} JSON).
#
# The address comes from ~/.config/omnigent/omnigent.env (OMNIGENT_BIND/PORT), so this works
# on a host that publishes somewhere other than the default 127.0.0.1:8000.
set -uo pipefail

ENV_FILE="${HOME}/.config/omnigent/omnigent.env"
BIND="$(sed -n 's/^OMNIGENT_BIND=//p' "${ENV_FILE}" 2>/dev/null | tail -1)"
PORT="$(sed -n 's/^OMNIGENT_PORT=//p' "${ENV_FILE}" 2>/dev/null | tail -1)"
BASE="http://${BIND:-127.0.0.1}:${PORT:-8000}"

PASS_N=0; FAIL_N=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS_N=$((PASS_N+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL_N=$((FAIL_N+1)); }

echo "== Container liveness =="
for c in omnigent-postgres omnigent-server omnigent-runner; do
    if podman inspect "$c" >/dev/null 2>&1; then
        STATUS="$(podman inspect --format '{{.State.Status}}' $c)"
        HEALTH="$(podman inspect --format '{{.State.Health.Status}}' $c 2>/dev/null || echo -)"
        if [ "${STATUS}" = "running" ]; then
            ok "$c running (health=${HEALTH})"
        else
            bad "$c not running (status=${STATUS})"
        fi
    else
        bad "$c container not found"
    fi
done

echo
echo "== Postgres reachable inside its own container =="
if podman exec omnigent-postgres pg_isready -U omnigent -d omnigent >/dev/null 2>&1; then
    ok "pg_isready OK"
else
    bad "pg_isready failed"
fi

echo
echo "== Server HTTP surface (${BASE}) =="
CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${BASE}/health")"
if [ "${CODE}" = "200" ]; then ok "/health -> 200"; else bad "/health -> ${CODE}"; fi

CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${BASE}/")"
case "${CODE}" in
    200|302|307) ok "/ -> ${CODE} (web UI reachable)" ;;
    *)           bad "/ -> ${CODE}" ;;
esac

echo
printf '  %d passed, %d failed\n\n' "${PASS_N}" "${FAIL_N}"
[ "${FAIL_N}" -eq 0 ] || exit 1
