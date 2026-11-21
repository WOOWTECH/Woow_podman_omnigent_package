#!/usr/bin/env python3
"""Private synthetic MCP fixtures for the disposable rootless acceptance job.

Only fixed status/error messages reach stdout. URLs, passwords, cookies and dumps stay in
private temporary files, never in an uploaded artifact. This does not call an LLM tool.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:18000"
PROFILE_API = BASE + "/v1/admin/mcp-server/profiles"
SECRET_NAMES = ("omnigent-postgres-password", "omnigent-admin-password",
                "omnigent-runner-password", "omnigent-database-url")
MARKERS = (("omnigent-server", "/data/artifacts/rootless-ci-marker"),
           ("omnigent-runner", "/data/pi-agent/rootless-ci-marker"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def secret(name):
    return json.loads(subprocess.check_output(
        ["podman", "secret", "inspect", "--showsecret", "--format", "{{json .SecretData}}", name],
        text=True, stderr=subprocess.DEVNULL))


def secret_hashes():
    return {name: hashlib.sha256(secret(name).encode()).hexdigest() for name in SECRET_NAMES}


def request(url, method="GET", data=None, token=None):
    require(url.startswith(BASE + "/"), "request must remain on the disposable loopback service")
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except HTTPError as exc:
        return exc.code, None


def login():
    status, body = request(BASE + "/auth/login", "POST", {"username": "admin", "password": secret("omnigent-admin-password")})
    require(status == 200 and isinstance(body.get("token"), str), "admin login failed")
    return body["token"]


def create(token, name):
    status, body = request(PROFILE_API, "POST", {"name": name, "username": "admin", "password": secret("omnigent-admin-password")}, token)
    require(status == 201, "profile creation failed")
    require(body["connection_url"].startswith(BASE + "/mcp/private_"), "canonical MCP base differs")
    return {"id": body["profile"]["id"], "url": body["connection_url"]}


def revoke(token, profile):
    require(request(PROFILE_API + "/" + profile["id"], "DELETE", token=token)[0] == 204, "profile revocation failed")


def probe(profile, active):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "rootless-ci", "version": "1"}}}
    status, body = request(profile["url"], "POST", payload)
    if not active:
        require(status in (401, 403, 404), "revoked profile unexpectedly accepted")
        return
    require(status == 200 and "result" in body, "active profile initialization failed")
    status, body = request(profile["url"], "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    require(status == 200, "MCP tools listing failed")
    names = {tool["name"] for tool in body["result"]["tools"]}
    require(len(names) == 15 and "omnigent_session_history" in names, "native MCP tool contract differs")


def save(path, state):
    path.write_text(json.dumps(state))
    path.chmod(0o600)


def marker_write(value):
    for container, path in MARKERS:
        subprocess.run(["podman", "exec", container, "sh", "-c", 'printf %s "$1" > "$2"', "marker", value, path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cookie_hash():
    return subprocess.check_output(["podman", "exec", "omnigent-server", "sha256sum", "/data/accounts-cookie-secret"],
                                   text=True, stderr=subprocess.DEVNULL).split()[0]


def prepare(path):
    token = login()
    state = {"active": create(token, "rootless-retained"), "revoked": create(token, "rootless-revoked")}
    revoke(token, state["revoked"])
    state["secrets"] = secret_hashes()
    state["cookie"] = cookie_hash()
    marker_write("checkpoint")
    save(path, state)
    check(path)


def check(path, restored=False):
    state = json.loads(path.read_text())
    probe(state["active"], True)
    probe(state["revoked"], False)
    status, body = request(PROFILE_API, token=login())
    require(status == 200, "profile list failed")
    rows = {p["id"]: p for p in body["profiles"]}
    require(rows[state["active"]["id"]]["active"] is True, "active profile state lost")
    require(rows[state["revoked"]["id"]]["active"] is False, "revoked profile state lost")
    if restored:
        require(state["afterCheckpoint"]["id"] not in rows, "database restore retained post-checkpoint data")
        probe(state["afterCheckpoint"], False)
    require(secret_hashes() == state["secrets"], "password secrets changed")
    require(cookie_hash() == state["cookie"], "cookie secret changed")
    for container, marker in MARKERS:
        value = subprocess.check_output(["podman", "exec", container, "cat", marker], stderr=subprocess.DEVNULL)
        require(value == b"checkpoint", "server/private Pi artifact checkpoint differs")


def mutate(path):
    state = json.loads(path.read_text())
    token = login()
    revoke(token, state["active"])
    state["afterCheckpoint"] = create(token, "rootless-after-checkpoint")
    probe(state["active"], False)
    probe(state["afterCheckpoint"], True)
    marker_write("changed-after-checkpoint")
    save(path, state)


def browser():
    env = {**os.environ, "OMNIGENT_BASE_URL": BASE, "OMNIGENT_MCP_PUBLIC_BASE_URL": BASE,
           "OMNIGENT_ADMIN_USERNAME": "admin", "OMNIGENT_ADMIN_PASSWORD": secret("omnigent-admin-password")}
    result = subprocess.run(["npx", "playwright", "test", "specs/mcp.spec.ts", "--retries=0", "--reporter=line"],
                            cwd=Path(__file__).parent / "e2e", env=env, capture_output=True)
    require(result.returncode == 0, "browser MCP creation/connection/revocation failed; credential-bearing output withheld")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check", "mutate", "restored", "browser"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "browser": browser()
        elif args.phase == "restored": check(args.state, restored=True)
        else: {"prepare": prepare, "check": check, "mutate": mutate}[args.phase](args.state)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, URLError, subprocess.SubprocessError):
        # Never echo an exception: network errors can include the private MCP URL.
        print("FAIL rootless MCP phase " + args.phase + " (details withheld)", file=sys.stderr)
        return 1
    print("PASS rootless MCP phase " + args.phase)
    return 0


if __name__ == "__main__":
    sys.exit(main())
