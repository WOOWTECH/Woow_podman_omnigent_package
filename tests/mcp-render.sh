#!/usr/bin/env bash
# Exercise the real renderer without needing Podman, containers, or a user manager.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../scripts/render-args.sh
. "$REPO/scripts/render-args.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export QL_ENV_MODE_CHECK=0
for mode in private shared; do
  for value in absent empty explicit; do
    envfile=$WORK/$mode-$value.env
    sed '/^OMNIGENT_MCP_PUBLIC_BASE_URL=/d; /^OMNIGENT_PI_STATE=/d' "$REPO/config/omnigent.env.example" >"$envfile"
    printf 'OMNIGENT_PI_STATE=%s\n' "$mode" >>"$envfile"
    expected=''
    case $value in
      empty) printf 'OMNIGENT_MCP_PUBLIC_BASE_URL=\n' >>"$envfile" ;;
      explicit)
        expected=https://agents.example.com/deployment-prefix/
        printf 'OMNIGENT_MCP_PUBLIC_BASE_URL=%s\nOMNIGENT_WEB_BASE_PATH=/must-not-append\n' "$expected" >>"$envfile" ;;
    esac
    ql_env_load "$envfile"
    render_args "$envfile"
    ql_render "$REPO/quadlet" "$envfile" "$REPO/quadlet/render-vars" "$WORK/$mode-$value" "${RENDER_ARGS[@]}"
    grep -qxF "Environment=OMNIGENT_MCP_PUBLIC_BASE_URL=$expected" "$WORK/$mode-$value/omnigent-server.container"
    volume=omnigent-pi.volume
    [[ $mode == private ]] || volume=pi-agent-data.volume
    grep -qF "Volume=$volume:/data/pi-agent" "$WORK/$mode-$value/omnigent-runner.container"
    echo "ok   $mode pi state, $value MCP base URL preserved exactly"
  done
done
