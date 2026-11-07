#!/usr/bin/env bash
# Ensure the daily, non-admin account exists and matches its podman secret.
# Input: BASE, OMNIGENT_ADMIN_USERNAME, OMNIGENT_RUNNER_USERNAME.
# Passwords travel from podman secret inspect through stdin to jq and curl.
set -euo pipefail
: "${BASE:?BASE is required}"
: "${OMNIGENT_ADMIN_USERNAME:?admin username is required}"
: "${OMNIGENT_RUNNER_USERNAME:?operator username is required}"
[[ $OMNIGENT_ADMIN_USERNAME != "$OMNIGENT_RUNNER_USERNAME" ]] || { echo 'operator must differ from admin' >&2; exit 1; }

work=$(mktemp -d "${TMPDIR:-/tmp}/omnigent-operator.XXXXXX")
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

secret() { podman secret inspect --showsecret --format '{{.SecretData}}' "$1"; }
login() { # login <username> <secret name> <output file>
  local user=$1 name=$2 output=$3
  secret "$name" | jq -Rn --arg u "$user" '{username:$u,password:input}' \
    | curl -sS -m 15 -o "$output" -w '%{http_code}' -H 'Content-Type: application/json' \
      --data-binary @- "$BASE/auth/login" 2>/dev/null
}

operator_code=$(login "$OMNIGENT_RUNNER_USERNAME" omnigent-runner-password "$work/operator.json") || operator_code=000
if [[ $operator_code == 200 ]]; then
  jq -e --arg u "$OMNIGENT_RUNNER_USERNAME" \
    '.user.id == $u and .user.is_admin == false and (.token | length > 0)' \
    "$work/operator.json" >/dev/null \
    || { echo "operator '$OMNIGENT_RUNNER_USERNAME' has unexpected permissions" >&2; exit 1; }
  echo "operator '$OMNIGENT_RUNNER_USERNAME' already exists and can log in"
  exit 0
fi

# Only 401 is an ordinary "not created yet" response. A rate limit, outage, or malformed
# response needs an operator, not an invitation that would hide the original problem.
[[ $operator_code == 401 ]] || { echo "operator login returned HTTP $operator_code" >&2; exit 1; }
admin_code=$(login "$OMNIGENT_ADMIN_USERNAME" omnigent-admin-password "$work/admin.json") || admin_code=000
if [[ $admin_code != 200 ]] || ! jq -e '.user.is_admin == true and (.token | length > 0)' "$work/admin.json" >/dev/null; then
  echo "admin '$OMNIGENT_ADMIN_USERNAME' cannot log in with omnigent-admin-password (HTTP $admin_code)" >&2
  exit 1
fi

# curl reads the Authorization header from a private file, keeping the JWT out of argv.
jq -r '"Authorization: Bearer " + .token' "$work/admin.json" >"$work/header"
chmod 600 "$work/header"
invite_code=$(curl -sS -m 15 -o "$work/invite.json" -w '%{http_code}' -X POST \
  -H "@$work/header" -H 'Content-Type: application/json' -d '{"is_admin":false}' \
  "$BASE/auth/invite" 2>/dev/null) || invite_code=000
if [[ $invite_code != 200 ]] || ! jq -e '.is_admin == false and (.token | length > 0)' "$work/invite.json" >/dev/null; then
  echo "cannot invite operator (HTTP $invite_code)" >&2
  exit 1
fi
invite=$(jq -r '.token' "$work/invite.json")
register_code=$({ printf '%s\n' "$invite"; secret omnigent-runner-password; } \
  | jq -Rn --arg u "$OMNIGENT_RUNNER_USERNAME" \
      '{invite:input,username:$u,password:input}' \
  | curl -sS -m 15 -o "$work/register.json" -w '%{http_code}' \
      -H 'Content-Type: application/json' --data-binary @- "$BASE/auth/register" 2>/dev/null) || register_code=000
if [[ $register_code != 200 ]] || ! jq -e --arg u "$OMNIGENT_RUNNER_USERNAME" \
  '.user.id == $u and .user.is_admin == false' "$work/register.json" >/dev/null; then
  echo "cannot register operator '$OMNIGENT_RUNNER_USERNAME' (HTTP $register_code); if this account already exists, set its password to match omnigent-runner-password" >&2
  exit 1
fi
echo "created non-admin operator '$OMNIGENT_RUNNER_USERNAME'"
