#!/usr/bin/env bash
# tests/runner-login.sh: the runner's login helper and its watchdog, against doubles.
#
# No container, no omnigent install, no network beyond loopback. A throwaway HTTP server plays
# POST /auth/login, a stub `omnigent.cli_auth` records what would be stored, and stub
# `omnigent`, `curl`, `pi` and `sleep` commands stand in for the CLI inside omnigent-runner-loop.
#
#   login     omnigent-runner-login asks for a refresh grant, stores token + user id + grant
#             through store_token, exits 2 when the server refuses and 3 when it cannot be
#             reached or answers an unexpected shape, and never prints the password, the token
#             or a response body (the real server echoes the input back on a 422)
#   --ensure  does nothing while the stored token is good, renews it from the grant, and logs
#             in again only when it cannot be renewed
#   loop      omnigent-runner-loop registers the host, keeps calling `--ensure`, starts the
#             host daemon again when `host status` lists it as offline (it keeps listing a
#             dead daemon, so "something is listed" is not "it is running"), follows the new
#             daemon's log file, gives up on a login that is refused for good, and exits on TERM
#
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
LOGIN=$REPO/rootfs/usr/local/bin/omnigent-runner-login
LOOP=$REPO/rootfs/usr/local/bin/omnigent-runner-loop
for f in "$LOGIN" "$LOOP"; do [[ -x $f ]] || { echo "runner-login: $f is missing or not executable" >&2; exit 1; }; done
command -v python3 >/dev/null || { echo "runner-login: python3 is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "runner-login: jq is required" >&2; exit 1; }

T=$(mktemp -d "${TMPDIR:-/tmp}/runner-login.XXXXXX")
SERVER_PID='' LOOP_PID=''
cleanup() {
  [[ -n $LOOP_PID ]] && kill "$LOOP_PID" 2>/dev/null
  [[ -n $SERVER_PID ]] && kill "$SERVER_PID" 2>/dev/null
  pkill -f "[t]ail -F $T/home" 2>/dev/null # a tail the loop left behind
  rm -rf "$T"
}
trap cleanup EXIT
npass=0 nfail=0
ok() { npass=$((npass + 1)); printf 'ok   %s\n' "$1"; }
fail() { nfail=$((nfail + 1)); printf 'FAIL %s\n' "$1" >&2; }
check() { # check <description> <command...>
  local d=$1
  shift
  if "$@" >/dev/null; then ok "$d"; else fail "$d"; fi
}
count() { # count <fixed string> <file>: matching lines, 0 when there is no file
  local n
  n=$(grep -cF -- "$1" "$2" 2>/dev/null) || true
  echo "${n:-0}"
}
wait_for() { # wait_for <seconds> <command...>
  local i=0 max=$(($1 * 10))
  shift
  until "$@"; do
    i=$((i + 1))
    ((i < max)) || return 1
    sleep 0.1
  done
}

# --- doubles --------------------------------------------------------------------------------
mkdir -p "$T/stub/omnigent" "$T/bin" "$T/home"
: >"$T/stub/omnigent/__init__.py"
cat >"$T/stub/omnigent/cli_auth.py" <<'PY'
import json, os, time
D = os.environ["STUB_DIR"]
REFRESH_MIN_REMAINING_SECONDS = 60
def store_token(server_url, token, user_id, expires_at, refresh_token=None):
    with open(D + "/store.log", "a") as f:
        f.write(json.dumps({"server": server_url, "token": token, "user_id": user_id,
                            "expires_in": round(expires_at - time.time()),
                            "refresh_token": refresh_token}) + "\n")
def load_token(server_url, min_remaining_seconds=0):
    return "valid-token" if os.path.exists(D + "/valid") else None
def refresh_stored_token(server_url):
    return "renewed-token" if os.path.exists(D + "/renewable") else None
PY
cat >"$T/server.py" <<'PY'
import http.server, json, sys
D = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        user = body.get("username")
        with open(D + "/requests.log", "a") as f:
            f.write(json.dumps({"path": self.path, "user": user, "issue_refresh": body.get("issue_refresh")}) + "\n")
        if self.path != "/auth/login" or body.get("password") != "pw-" + str(user):
            return self.reply(422, {"detail": "rejected " + json.dumps(body)})  # echoes the input back
        if user == "weird":
            return self.reply(200, {"ok": True})
        out = {"token": "tok-" + user, "user": {"id": "id-" + user}, "expires_in": 28800}
        if user != "norefresh":
            out["refresh_token"] = "ref-" + user
        self.reply(200, out)
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
open(D + "/port", "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
python3 -I "$T/server.py" "$T" &
SERVER_PID=$!
wait_for 10 test -s "$T/port" || { echo "runner-login: the fake auth server did not start" >&2; exit 1; }
PORT=$(<"$T/port")

login() { # login <user> <password> [--ensure]: env -> exit status, output in $T/out
  local user=$1 pw=$2
  shift 2
  PYTHONPATH=$T/stub STUB_DIR=$T OMNIGENT_SERVER_URL="http://127.0.0.1:$PORT" \
    OMNIGENT_RUNNER_USERNAME="$user" OMNIGENT_RUNNER_PASSWORD="$pw" \
    "$LOGIN" "$@" >"$T/out" 2>&1
}
reset() { rm -f "$T/store.log" "$T/requests.log" "$T/valid" "$T/renewable"; }
out_lacks() { ! grep -qF -- "$1" "$T/out"; }

# --- login ----------------------------------------------------------------------------------
reset
login good pw-good
check "login: exit 0 on a good account" test $? -eq 0
check "login: asks for a refresh grant" grep -q '"issue_refresh": true' "$T/requests.log"
check "login: stores token, user id and refresh grant through store_token" \
  jq -e '.token == "tok-good" and .user_id == "id-good" and .refresh_token == "ref-good" and (.expires_in | . > 28700 and . <= 28800)' "$T/store.log"
check "login: says the grant is stored" grep -q 'refresh grant stored' "$T/out"
check "login: prints neither the password nor the token" bash -c "! grep -qE 'pw-good|tok-good|ref-good' '$T/out'"

reset
login norefresh pw-norefresh
check "login: still stores a login when the server issues no grant, and says so" \
  bash -c "grep -q 'NOT issued' '$T/out' && jq -e '.refresh_token == null' '$T/store.log'"

reset
login good wrong-password
rc=$?
check "login: exit 2 when the server refuses the credentials" test $rc -eq 2
check "login: stores nothing when refused" test ! -e "$T/store.log"
check "login: does not print the password, or the server's echo of it" bash -c "! grep -qE 'wrong-password|rejected' '$T/out'"

reset
login weird pw-weird
check "login: exit 3 on a reply of an unexpected shape" test $? -eq 3
check "login: stores nothing for that reply" test ! -e "$T/store.log"

reset
good_pw="pw-good" # kept in a variable: tests/dryrun.local.sh rejects a literal PASSWORD=value
PYTHONPATH=$T/stub STUB_DIR=$T OMNIGENT_SERVER_URL="http://127.0.0.1:9" OMNIGENT_RUNNER_USERNAME=good \
  OMNIGENT_RUNNER_PASSWORD=$good_pw "$LOGIN" >"$T/out" 2>&1
check "login: exit 3 when the server cannot be reached" test $? -eq 3

reset
PYTHONPATH=$T/stub STUB_DIR=$T OMNIGENT_SERVER_URL="http://127.0.0.1:$PORT" OMNIGENT_ADMIN_USERNAME=good \
  OMNIGENT_ADMIN_PASSWORD=$good_pw "$LOGIN" >"$T/out" 2>&1
check "login: falls back to OMNIGENT_ADMIN_{USERNAME,PASSWORD}" jq -e '.user_id == "id-good"' "$T/store.log"

reset
PYTHONPATH=$T/stub STUB_DIR=$T OMNIGENT_SERVER_URL="http://127.0.0.1:$PORT" "$LOGIN" >"$T/out" 2>&1
check "login: exit 2 without an account" test $? -eq 2

# --- --ensure -------------------------------------------------------------------------------
reset
touch "$T/valid"
login good pw-good --ensure
check "--ensure: a good stored token costs no request" bash -c "test \$? -eq 0 -a ! -e '$T/requests.log' -a ! -e '$T/store.log'" _

reset
touch "$T/renewable"
login good pw-good --ensure
check "--ensure: a renewable token is renewed from the grant, with no login" \
  bash -c "grep -q 'renewed the access token' '$T/out' && test ! -e '$T/requests.log'"

reset
login good pw-good --ensure
check "--ensure: an unrenewable token means logging in again" \
  bash -c "grep -q 'logging in again' '$T/out' && jq -e '.token == \"tok-good\"' '$T/store.log'"

# --- the loop -------------------------------------------------------------------------------
# `sleep` is shortened so the 10 s login retry and the 60 s watchdog tick take milliseconds.
cat >"$T/bin/sleep" <<'SH'
#!/bin/sh
exec /bin/sleep 0.05
SH
cat >"$T/bin/curl" <<'SH'
#!/bin/sh
exit 0
SH
cat >"$T/bin/pi" <<'SH'
#!/bin/sh
echo "0.0.0-stub"
SH
cat >"$T/bin/omnigent" <<'SH'
#!/bin/sh
# Records the calls that matter; `host status` answers from $STUB_DIR/alive the way the real one
# does: a daemon whose process died is still listed, as "process": "offline".
case "$*" in
  --version) echo "omnigent 0.0.0 (stub)" ;;
  "host status"*)
    if [ -e "$STUB_DIR/alive" ]; then p=online; else p=offline; fi
    printf '{"daemons":[{"pid":20,"process":"%s","host_status":"%s"}]}\n' "$p" "$p"
    ;;
  "host --background"*)
    echo "omnigent $*" >>"$STUB_DIR/calls.log"
    n=$(($(cat "$STUB_DIR/starts" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$STUB_DIR/starts"
    mkdir -p "$HOME/.omnigent/logs/host"
    echo "daemon $n up" >"$HOME/.omnigent/logs/host/host-$n.log"
    touch "$STUB_DIR/alive"
    echo "Started the host daemon in the background (pid 20)."
    ;;
esac
SH
cat >"$T/bin/omnigent-runner-login" <<'SH'
#!/bin/sh
echo "login $*" >>"$STUB_DIR/calls.log"
exit "$(cat "$STUB_DIR/login_rc" 2>/dev/null || echo 0)"
SH
chmod +x "$T/bin/"*

run_loop() { # run_loop <user> <password>: background, output in $T/loop.out
  rm -f "$T/calls.log" "$T/starts" "$T/alive" "$T/login_rc" "$T/loop.out"
  rm -rf "$T/home/.omnigent"
  env PATH="$T/bin:$PATH" HOME="$T/home" STUB_DIR="$T" OMNIGENT_SERVER_URL="http://stub:8000" \
    ${1:+OMNIGENT_RUNNER_USERNAME=$1} ${2:+OMNIGENT_RUNNER_PASSWORD=$2} \
    sh "$LOOP" >"$T/loop.out" 2>&1 &
  LOOP_PID=$!
}
starts() { count 'host --background' "$T/calls.log"; }
starts_ge() { (($(starts) >= $1)); }
logged() { grep -qF -- "$1" "$T/loop.out"; }

run_loop runner pw
check "loop: registers the host once the login succeeds" wait_for 10 starts_ge 1
check "loop: logged in before it registered" bash -c "head -1 '$T/calls.log' | grep -q '^login'"
check "loop: follows the first daemon's log" wait_for 10 logged "host-1.log"
check "loop: the watchdog keeps calling --ensure" wait_for 10 grep -q 'login --ensure' "$T/calls.log"
sleep 0.5
check "loop: leaves a running daemon alone" test "$(starts)" -eq 1
rm -f "$T/alive" # the daemon process dies; `host status` still lists it, as offline
check "loop: starts the daemon again when it is listed as offline" wait_for 10 starts_ge 2
check "loop: says so" wait_for 10 logged "host daemon is gone"
check "loop: follows the restarted daemon's new log file" wait_for 10 logged "host-2.log"
sleep 0.5
check "loop: and stops restarting once it is up" test "$(starts)" -eq 2
kill -TERM "$LOOP_PID"
check "loop: exits on TERM" wait_for 10 bash -c "! kill -0 $LOOP_PID 2>/dev/null"
wait "$LOOP_PID" 2>/dev/null
check "loop: leaves no tail behind" bash -c "! pgrep -f '[t]ail -F $T/home' >/dev/null"
LOOP_PID=''

run_loop runner pw
echo 2 >"$T/login_rc"
wait "$LOOP_PID"
rc=$?
LOOP_PID=''
check "loop: gives up with status 1 on a login that is refused for good" test $rc -eq 1
check "loop: never registers a host without a login" test "$(starts)" -eq 0

run_loop '' ''
wait "$LOOP_PID"
LOOP_PID=''
check "loop: without an account it does not register either" bash -c "test \"\$(grep -cF 'host --background' '$T/calls.log' 2>/dev/null)\" != 1 && grep -q 'cannot auto-register' '$T/loop.out'"

printf '\n%s passed, %s failed\n' "$npass" "$nfail"
((nfail == 0))
