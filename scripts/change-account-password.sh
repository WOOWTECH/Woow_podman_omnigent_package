#!/usr/bin/env bash
# Change an accounts-mode password via POST /auth/users/me/password.
# Input: BASE, USER_NAME, SECRET_NAME; new password is read from stdin.
# The current password comes from the podman secret and never reaches argv or logs.
set -euo pipefail
: "${BASE:?BASE is required}"
: "${USER_NAME:?USER_NAME is required}"
: "${SECRET_NAME:?SECRET_NAME is required}"
IFS= read -r new_password || { echo 'new password missing on stdin' >&2; exit 1; }
[[ ${#new_password} -ge 8 ]] || { echo 'new password must be at least eight characters' >&2; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/omnigent-password.XXXXXX")
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
current=$(podman secret inspect --showsecret --format '{{.SecretData}}' "$SECRET_NAME")

login_code=$(printf '%s\n' "$current" \
  | jq -Rn --arg u "$USER_NAME" '{username:$u,password:input}' \
  | curl -sS -m 15 -o "$work/login.json" -w '%{http_code}' \
      -H 'Content-Type: application/json' --data-binary @- "$BASE/auth/login" 2>/dev/null) || login_code=000
if [[ $login_code != 200 ]] || ! jq -e --arg u "$USER_NAME" '.user.id == $u and (.token | length > 0)' "$work/login.json" >/dev/null; then
  echo "current password secret for '$USER_NAME' cannot log in (HTTP $login_code)" >&2
  exit 1
fi
jq -r '"Authorization: Bearer " + .token' "$work/login.json" >"$work/header"
chmod 600 "$work/header"
change_code=$(printf '%s\n%s\n' "$current" "$new_password" \
  | jq -Rn '{old_password:input,new_password:input}' \
  | curl -sS -m 15 -o "$work/change.json" -w '%{http_code}' -X POST \
      -H "@$work/header" -H 'Content-Type: application/json' --data-binary @- \
      "$BASE/auth/users/me/password" 2>/dev/null) || change_code=000
[[ $change_code == 204 ]] || { echo "password change for '$USER_NAME' failed (HTTP $change_code)" >&2; exit 1; }
echo "changed the password of '$USER_NAME' in Omnigent"
