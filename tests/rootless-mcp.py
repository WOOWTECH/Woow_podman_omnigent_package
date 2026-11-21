#!/usr/bin/env python3
"""Private synthetic MCP fixtures for the disposable rootless acceptance job.

Only fixed status/error messages reach stdout. URLs, passwords, cookies and dumps stay in
private temporary files, never in an uploaded artifact. This does not call an LLM tool.
"""
import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:18000"
PROFILE_API = BASE + "/v1/admin/mcp-server/profiles"
SECRET_NAMES = ("omnigent-postgres-password", "omnigent-admin-password",
                "omnigent-runner-password", "omnigent-database-url")
FAILURE_STEPS = frozenset({"dispatch", "login", "create", "revoke", "secret-hash", "cookie",
                           "marker", "probe", "profile-list", "state", "browser", "inventory", "storage", "peer"})
current_step = "dispatch"
MARKERS = (("omnigent-server", "/data/artifacts/rootless-ci-marker"),
           ("omnigent-runner", "/data/pi-agent/rootless-ci-marker"))


def run_step(code, action, *args, **kwargs):
    global current_step
    if code not in FAILURE_STEPS:
        raise ValueError("unknown fixture step")
    current_step = code
    return action(*args, **kwargs)


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
        subprocess.run(["podman", "exec", container, "sh", "-c", 'mkdir -p -- "${2%/*}" && printf %s "$1" > "$2"', "marker", value, path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)



def pi_mode():
    mode = os.environ.get("OMNIGENT_ROOTLESS_PI_STATE", "private")
    require(mode in ("private", "shared"), "invalid Pi fixture mode")
    return mode


def peer_identity():
    return subprocess.check_output(["podman", "inspect", "--format", "{{.Id}}|{{.State.StartedAt}}",
                                    "omnigent-rootless-peer"], text=True, stderr=subprocess.DEVNULL).strip()


def peer_write(value):
    subprocess.run(["podman", "exec", "omnigent-rootless-peer", "sh", "-c",
                    'printf %s "$1" > /data/pi-agent/rootless-ci-marker', "marker", value],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def peer_check(state, value):
    require(peer_identity() == state["peerIdentity"], "shared peer was recreated or restarted")
    for container in ("omnigent-rootless-peer", "omnigent-runner"):
        actual = subprocess.check_output(["podman", "exec", container, "cat", MARKERS[1][1]], stderr=subprocess.DEVNULL)
        require(actual == value, "shared filesystem marker differs")


def storage_prepare():
    state = {"piState": pi_mode(), "secrets": run_step("secret-hash", secret_hashes),
             "cookie": run_step("cookie", cookie_hash)}
    run_step("marker", marker_write, "checkpoint")
    if pi_mode() == "shared":
        state["peerIdentity"] = run_step("peer", peer_identity)
        run_step("peer", peer_check, state, b"checkpoint")
        # Prove writes in both directions, without running a full Pi web application.
        run_step("peer", peer_write, "peer-wrote-checkpoint")
        run_step("peer", peer_check, state, b"peer-wrote-checkpoint")
        run_step("peer", peer_write, "checkpoint")
    return state


def storage_check(state, restored=False):
    require(state["piState"] == pi_mode(), "fixture mode changed")
    require(run_step("secret-hash", secret_hashes) == state["secrets"], "password secrets changed")
    require(run_step("cookie", cookie_hash) == state["cookie"], "cookie secret changed")
    for container, marker in MARKERS:
        expected = b"external-after-checkpoint" if restored and pi_mode() == "shared" and container == "omnigent-runner" else b"checkpoint"
        value = run_step("marker", subprocess.check_output,
                         ["podman", "exec", container, "cat", marker], stderr=subprocess.DEVNULL)
        require(value == expected, "server/private Pi artifact checkpoint differs")
    if pi_mode() == "shared":
        run_step("peer", peer_check, state, b"external-after-checkpoint" if restored else b"checkpoint")


def stopped_writers():
    repo = Path(__file__).resolve().parents[1]
    subprocess.run(["bash", "-c", '. "$1/scripts/lib/quadlet-lib.sh"; . "$1/scripts/writer-state.sh"; omnigent_assert_writers_stopped',
                    "fixture", str(repo)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def storage_mutate():
    stopped_writers()
    image = "localhost/woow-omnigent-runner:0.21.0-2"
    # Only the fixture marker and the persisted cookie secret are modified on the
    # stopped package volumes. The live peer owns writes to the shared volume.
    subprocess.run(["podman", "run", "--rm", "--network=none", "--entrypoint=/bin/sh",
                    "-v", "omnigent-server-data:/fixture", image, "-c",
                    'printf changed > /fixture/artifacts/rootless-ci-marker && printf %s "$1" > /fixture/artifacts/accounts-cookie-secret',
                    "fixture", secrets.token_hex(32)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if pi_mode() == "private":
        subprocess.run(["podman", "run", "--rm", "--network=none", "--userns=keep-id:uid=1000,gid=1000",
                        "--entrypoint=/bin/sh", "-v", "omnigent-pi-data:/fixture", image, "-c",
                        'printf changed > /fixture/rootless-ci-marker'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        peer_write("external-after-checkpoint")
    # Restore must replace actual secrets, not merely retain unchanged hashes.
    for name in SECRET_NAMES:
        subprocess.run(["podman", "secret", "create", "--replace", name, "-"],
                       input=secrets.token_hex(24).encode(), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stopped_writers()


def inventory(path):
    mode = pi_mode()
    metadata = json.loads((path / "checkpoint.json").read_text())
    require(metadata["piState"] == mode, "checkpoint mode differs")
    expected = {"checkpoint.json", "omnigent.env", "units", "secrets.json", "secrets.json.sha256", "writer-states.tsv"}
    patterns = ["omnigent-*.dump", "omnigent-server-data-*.tar"]
    if mode == "private": patterns.append("omnigent-pi-data-*.tar")
    for pattern in patterns:
        matches = list(path.glob(pattern))
        require(len(matches) == 1, "checkpoint inventory differs")
        expected.update((matches[0].name, matches[0].name + ".sha256"))
    require({p.name for p in path.iterdir()} == expected, "unexpected checkpoint content")
    require(all("pi-agent-data" not in key and "peer" not in key for key in metadata["units"]),
            "external shared resources entered checkpoint")
    saved_units = {p.relative_to(path / "units").as_posix() for p in (path / "units").rglob("*") if p.is_file()}
    require(saved_units == {key for key, value in metadata["units"].items() if value is not None},
            "checkpoint unit files differ")
    require(not any(p.is_symlink() for p in path.rglob("*")), "checkpoint contains a symlink")
    require(list(path.parent.iterdir()) == [path], "ambiguous checkpoint directory")


def checkpoint_seed(path):
    save(path, storage_prepare())


def checkpoint_restored(path):
    storage_check(json.loads(path.read_text()), restored=True)


def cookie_hash():
    return subprocess.check_output(["podman", "exec", "omnigent-server", "sha256sum", "/data/artifacts/accounts-cookie-secret"],
                                   text=True, stderr=subprocess.DEVNULL).split()[0]


def prepare(path):
    token = run_step("login", login)
    state = {"active": run_step("create", create, token, "rootless-retained"),
             "revoked": run_step("create", create, token, "rootless-revoked")}
    run_step("revoke", revoke, token, state["revoked"])
    state.update(storage_prepare())
    run_step("state", save, path, state)
    check(path)


def check(path, restored=False):
    state = run_step("state", lambda: json.loads(path.read_text()))
    run_step("probe", probe, state["active"], True)
    run_step("probe", probe, state["revoked"], False)
    status, body = run_step("profile-list", request, PROFILE_API, token=run_step("login", login))
    require(status == 200, "profile list failed")
    rows = {p["id"]: p for p in body["profiles"]}
    require(rows[state["active"]["id"]]["active"] is True, "active profile state lost")
    require(rows[state["revoked"]["id"]]["active"] is False, "revoked profile state lost")
    if restored:
        require(state["afterCheckpoint"]["id"] not in rows, "database restore retained post-checkpoint data")
        run_step("probe", probe, state["afterCheckpoint"], False)
    storage_check(state, restored=restored)


def mutate(path):
    state = run_step("state", lambda: json.loads(path.read_text()))
    token = run_step("login", login)
    run_step("revoke", revoke, token, state["active"])
    state["afterCheckpoint"] = run_step("create", create, token, "rootless-after-checkpoint")
    run_step("probe", probe, state["active"], False)
    run_step("probe", probe, state["afterCheckpoint"], True)
    run_step("marker", marker_write, "changed-after-checkpoint")
    run_step("state", save, path, state)

def browser():
    env = {**os.environ, "OMNIGENT_BASE_URL": BASE, "OMNIGENT_MCP_PUBLIC_BASE_URL": BASE,
           "OMNIGENT_ADMIN_USERNAME": "admin", "OMNIGENT_ADMIN_PASSWORD": secret("omnigent-admin-password")}
    result = subprocess.run(["npx", "playwright", "test", "specs/mcp.spec.ts", "--retries=0", "--reporter=line"],
                            cwd=Path(__file__).parent / "e2e", env=env, capture_output=True)
    require(result.returncode == 0, "browser MCP creation/connection/revocation failed; credential-bearing output withheld")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check", "mutate", "restored", "browser", "checkpoint-seed", "checkpoint-restored", "storage-mutate", "inventory"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        if args.phase == "inventory": run_step("inventory", inventory, args.state)
        elif args.phase == "checkpoint-seed": run_step("storage", checkpoint_seed, args.state)
        elif args.phase == "checkpoint-restored": run_step("storage", checkpoint_restored, args.state)
        elif args.phase == "storage-mutate": run_step("storage", storage_mutate)
        elif args.phase == "browser": run_step("browser", browser)
        elif args.phase == "restored": check(args.state, restored=True)
        else: run_step("dispatch", {"prepare": prepare, "check": check, "mutate": mutate}[args.phase], args.state)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, URLError, subprocess.SubprocessError):
        # Never echo an exception: network errors can include the private MCP URL.
        code = current_step if current_step in FAILURE_STEPS else "unknown"
        if failure_path := os.environ.get("OMNIGENT_ROOTLESS_FAILURE_FILE"):
            try:
                with open(failure_path, "w", encoding="ascii") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(code + "\n")
            except OSError:
                pass
        print("FAIL rootless MCP phase " + args.phase + " step " + code + " (details withheld)", file=sys.stderr)
        return 1
    print("PASS rootless MCP phase " + args.phase)
    return 0


if __name__ == "__main__":
    sys.exit(main())
