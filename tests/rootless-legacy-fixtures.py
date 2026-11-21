#!/usr/bin/env python3
"""Legacy data fixtures for disposable GitHub-hosted rootless acceptance.

CLI: {seed,verify,inspect,mutate} PRIVATE_STATE --expected-head REV --mcp {absent,present}
A: d0a1f879a01d/absent; B: za3b4c5d6e7f/present. STATE's parent must be an
owned 0700 directory beneath RUNNER_TEMP; the new state file is created 0600.
seed/verify use the running server's installed stores and loopback HTTP API.
inspect reads PostgreSQL without the application. mutate requires stopped
application writers and changes only this fixture's database rows. The harness
owns volume mutation, secret rotation, backup/restore and resource cleanup.

State, subprocess output and HTTP responses are private. Only fixed status codes
reach the console. No inference, task dispatch, migration or restore is requested
by this helper. Never upload STATE. No external credentials are required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OLD_HEAD = "d0a1f879a01d"
NEW_HEAD = "za3b4c5d6e7f"
BASE = "http://127.0.0.1:18000"
CODES = {"arguments", "guard", "state", "schema", "stores", "accounts", "acl", "writers", "mutate"}
current_step = "arguments"

# Executed by python in the *installed* server image, not the checkout's Python.
# Input and output are private pipes. Direct stores create an already-paused task
# and unbound sessions, so seeding cannot invoke a provider or runner.
WORKER = r'''
import dataclasses, hashlib, io, json, os, sys, tarfile
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData, FunctionCallData, FunctionCallOutputData
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.passwords import hash_password
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

def require(ok):
    if not ok:
        raise ValueError("legacy-store-contract")

def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value

request = json.load(sys.stdin)
s = request["state"]
uri = os.environ["DATABASE_URL"]
# A fixture must never repair the schema it is supposed to verify. Fail before
# constructing stores if this installed package disagrees with the live DB.
from sqlalchemy import create_engine, text
from omnigent.db import utils as db_utils
probe = create_engine(db_utils.normalize_database_url(uri))
with probe.connect() as connection:
    require(connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            == [db_utils._get_head_db_revision(uri)])
probe.dispose()
def forbid_migration(*args, **kwargs):
    raise ValueError("legacy-fixture-must-not-migrate")
db_utils._run_migrations = forbid_migration
accounts = SqlAlchemyAccountStore(uri)
agents = SqlAlchemyAgentStore(uri)
conversations = SqlAlchemyConversationStore(uri)
permissions = SqlAlchemyPermissionStore(uri)
tasks = SqlAlchemyScheduledTaskStore(uri)
artifacts = LocalArtifactStore(os.environ["ARTIFACT_DIR"])
marker_key = "legacy-ci/" + s["nonce"] + "/marker.txt"
bundle_key = "legacy-ci/" + s["nonce"] + "/agent.tar.gz"

if request["operation"] == "seed":
    for role in ("owner", "viewer"):
        user = s[role]
        require(accounts.get_user(user["id"]) is None)
        accounts.create_user_with_password(user["id"], hash_password(user["password"]),
                                          is_admin=role == "owner")
    data = b"name: legacy-ci\nprompt: Never execute this fixture.\nexecutor:\n  model: gpt-4o-mini\n  harness: openai-agents\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        entry = tarfile.TarInfo("agent.yaml")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    artifacts.put(bundle_key, buf.getvalue())
    artifacts.put(marker_key, b"legacy fixture artifact\x00preserve bytes\n")
    agents.create(s["agent"], "legacy-" + s["nonce"], bundle_key,
                  description="Synthetic upgrade fixture; never execute")
    for visibility, sid in s["sessions"].items():
        conversations.create_conversation(conversation_id=sid, title="legacy-" + visibility,
                                          agent_id=s["agent"])
        permissions.grant(s["owner"]["id"], sid, 4)
        if visibility == "shared":
            permissions.grant(s["viewer"]["id"], sid, 1)
        items = []
        # Six ordered items cross a page boundary at limit=2. Tool calls are data.
        payloads = [
            ("message", MessageData(role="user", content=[{"type":"input_text", "text":"legacy user one"}])),
            ("message", MessageData(role="assistant", agent="legacy-ci", content=[{"type":"output_text", "text":"legacy assistant one"}])),
            ("function_call", FunctionCallData(agent="legacy-ci", name="synthetic_tool", arguments='{"fixture":true}', call_id="legacy-call")),
            ("function_call_output", FunctionCallOutputData(call_id="legacy-call", output="synthetic result; no tool executed")),
            ("message", MessageData(role="user", content=[{"type":"input_text", "text":"legacy user two"}])),
            ("message", MessageData(role="assistant", agent="legacy-ci", content=[{"type":"output_text", "text":"legacy assistant two"}])),
        ]
        for kind, data in payloads:
            items.append(NewConversationItem(type=kind, response_id="legacy-response", data=data,
                created_by=s["owner"]["id"] if kind == "message" and data.role == "user" else None))
        conversations.append(sid, items)
    tasks.create(s["task"], "legacy-paused", "Never run this fixture.",
                 "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1", s["owner"]["id"], s["agent"], "UTC",
                 state="paused", execution_target="connected_host")
elif request["operation"] != "verify":
    raise ValueError("legacy-worker-operation")

snapshot = {"accounts": {}, "sessions": {}, "agent": plain(agents.get(s["agent"])),
            "task": plain(tasks.get(s["task"])), "artifacts": {}}
for role in ("owner", "viewer"):
    user = accounts.get_user(s[role]["id"])
    require(user is not None and user.is_admin == (role == "owner"))
    snapshot["accounts"][role] = {k: getattr(user, k) for k in
        ("id", "is_admin", "created_at", "account_generation", "has_password")}
    snapshot["accounts"][role]["passwordHashDigest"] = hashlib.sha256(
        accounts.get_password_hash(user.id).encode()).hexdigest()
for visibility, sid in s["sessions"].items():
    conv = conversations.get_conversation(sid)
    require(conv is not None and conv.runner_id is None and conv.host_id is None)
    grants, cursor = permissions.list_for_session(sid)
    require(cursor is None)
    got = sorted((g.user_id, g.level) for g in grants)
    want = [(s["owner"]["id"], 4)]
    if visibility == "shared":
        want.append((s["viewer"]["id"], 1))
    require(got == sorted(want))
    items, after = [], None
    for _ in range(4):
        page = conversations.list_items(sid, limit=2, after=after, order="asc")
        items.extend(plain(item) for item in page.data)
        if not page.has_more:
            break
        require(page.last_id is not None and page.last_id != after)
        after = page.last_id
    require(not page.has_more and len(items) == 6 and len({i["id"] for i in items}) == 6)
    snapshot["sessions"][visibility] = {
        "conversation": {k: plain(getattr(conv, k)) for k in
            ("id", "title", "agent_id", "created_at", "labels", "runner_id", "host_id")},
        "grants": plain(grants), "items": items,
    }
require(snapshot["agent"] is not None)
require(snapshot["task"] is not None and snapshot["task"]["state"] == "paused")
require(snapshot["task"]["last_run_at"] is None and snapshot["task"]["last_run_conversation_id"] is None)
require(snapshot["task"]["next_run_at"] is None)
for key in (marker_key, bundle_key):
    snapshot["artifacts"][key] = hashlib.sha256(artifacts.get(key)).hexdigest()
print(json.dumps(snapshot, sort_keys=True))
'''

SCHEMA_SQL = """
SELECT json_build_object(
 'heads', (SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version),
 'table', EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname='public' AND c.relname='mcp_server_profiles' AND c.relkind='r'),
 'columns', (SELECT coalesce(json_agg(attname ORDER BY attname),'[]'::json) FROM pg_attribute
             WHERE attrelid=to_regclass('public.mcp_server_profiles') AND attnum>0 AND NOT attisdropped),
 'indexes', (SELECT coalesce(json_agg(json_build_object(
    'name', c.relname, 'unique', i.indisunique, 'primary', i.indisprimary,
    'valid', i.indisvalid, 'ready', i.indisready,
    'partial', i.indpred IS NOT NULL, 'expression', i.indexprs IS NOT NULL,
    'columns', (SELECT json_agg(a.attname ORDER BY k.ord)
      FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum,ord)
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum)
  ) ORDER BY c.relname),'[]'::json)
  FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
  WHERE i.indrelid=to_regclass('public.mcp_server_profiles'))
);
"""
MCP_COLUMNS = {"workspace_id", "id", "name", "user_id", "account_generation", "created_by",
               "token_hash", "created_at", "last_used_at", "revoked_at"}
MCP_INDEXES = {
    "mcp_server_profiles_pkey": (True, True, ["workspace_id", "id"]),
    "ux_mcp_server_profiles_token_hash": (True, False, ["workspace_id", "token_hash"]),
    "ix_mcp_server_profiles_user": (False, False, ["workspace_id", "user_id", "created_at"]),
}


class FixtureError(Exception):
    """Only a fixed failure code is displayed by main()."""


def require(ok):
    if not ok:
        raise FixtureError()


def step(code):
    global current_step
    require(code in CODES)
    current_step = code


def run(args, *, data=None, check=True):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=120)
    if check:
        require(result.returncode == 0)
    return result


def guard():
    step("guard")
    require(os.environ.get("GITHUB_ACTIONS") == "true")
    require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted")
    require(os.environ.get("RUNNER_OS") == "Linux" and os.getuid() != 0)
    require((os.environ.get("RUNNER_ARCH"), os.uname().machine) in
            {("X64", "x86_64"), ("ARM64", "aarch64")})
    require(run(["podman", "info", "--format", "{{.Host.Security.Rootless}}"])
            .stdout.strip() == "true")


def state_path(value):
    step("state")
    root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    path = Path(value).absolute()
    require(path.parent.resolve(strict=True) == path.parent)
    require(path.parent.is_relative_to(root) and path.parent != root)
    parent = path.parent.stat()
    require(parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700)
    require(not path.is_symlink())
    return path


def save_new(path, state):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def load_state(path):
    step("state")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600)
        require(info.st_size <= 1_000_000)
        state = json.load(stream)
    validate_state(state)
    return state


def validate_state(state):
    require(isinstance(state, dict) and state.get("format") == 1)
    require(re.fullmatch(r"[0-9a-f]{32}", state["nonce"]) is not None)
    require(set(state["sessions"]) == {"private", "shared"})
    ids = [state["agent"], state["task"], *state["sessions"].values()]
    require(len(set(ids)) == 4)
    require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in ids))
    for role in ("owner", "viewer"):
        require(state[role]["id"] == "legacy-" + role + "-" + state["nonce"])
        require(isinstance(state[role]["password"], str) and len(state[role]["password"]) >= 24)
    require(isinstance(state.get("snapshot"), dict))


def new_state():
    nonce = secrets.token_hex(16)
    return {"format": 1, "nonce": nonce,
            "owner": {"id": "legacy-owner-" + nonce, "password": secrets.token_urlsafe(24)},
            "viewer": {"id": "legacy-viewer-" + nonce, "password": secrets.token_urlsafe(24)},
            "agent": secrets.token_hex(16), "task": secrets.token_hex(16),
            "sessions": {v: secrets.token_hex(16) for v in ("private", "shared")}}


def psql(sql):
    return json.loads(run(["podman", "exec", "-i", "omnigent-postgres", "psql", "-X", "-qAt",
                           "-v", "ON_ERROR_STOP=1", "-U", "omnigent", "-d", "omnigent"], data=sql).stdout)


def check_schema(value, head, mcp):
    require((head, mcp) in {(OLD_HEAD, "absent"), (NEW_HEAD, "present")})
    require(value["heads"] == [head])
    require(value["table"] is (mcp == "present"))
    if mcp == "absent":
        require(value["columns"] == [] and value["indexes"] == [])
        return
    require(set(value["columns"]) == MCP_COLUMNS and len(value["columns"]) == len(MCP_COLUMNS))
    indexes = value["indexes"]
    require(len(indexes) == len(MCP_INDEXES))
    require({i["name"] for i in indexes} == set(MCP_INDEXES))
    for index in indexes:
        unique, primary, columns = MCP_INDEXES[index["name"]]
        require(index["unique"] is unique and index["primary"] is primary)
        require(index["valid"] is True and index["ready"] is True)
        require(index["partial"] is False and index["expression"] is False)
        require(index["columns"] == columns)


def inspect_schema(head, mcp):
    step("schema")
    check_schema(psql(SCHEMA_SQL), head, mcp)


def worker(operation, state):
    step("stores")
    return json.loads(run(["podman", "exec", "-i", "omnigent-server", "python3", "-I", "-c", WORKER],
                          data=json.dumps({"operation": operation, "state": state})).stdout)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FixtureError()


def request(path, *, body=None, token=None):
    require(path.startswith("/") and not path.startswith("//"))
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            data = response.read(2_000_000)
            return response.status, json.loads(data) if data else None
    except HTTPError as error:
        return error.code, None


def verify_http(state):
    step("accounts")
    tokens = {}
    for role in ("owner", "viewer"):
        user = state[role]
        status, payload = request("/auth/login", body={"username": user["id"], "password": user["password"]})
        require(status == 200 and isinstance(payload, dict) and isinstance(payload.get("token"), str))
        tokens[role] = payload["token"]
        status, me = request("/auth/me", token=tokens[role])
        require(status == 200 and me["id"] == user["id"] and me["is_admin"] is (role == "owner"))
    step("acl")
    for visibility, sid in state["sessions"].items():
        require(request("/v1/sessions/" + sid, token=tokens["owner"])[0] == 200)
        status, _ = request("/v1/sessions/" + sid, token=tokens["viewer"])
        require(status == 200 if visibility == "shared" else status in (403, 404))


def compare_snapshot(expected, actual):
    require(expected == actual)


def require_stopped_writers():
    step("writers")
    for name in ("omnigent-server", "omnigent-runner"):
        output = run(["systemctl", "--user", "show", name + ".service", "-p", "LoadState",
                      "-p", "ActiveState", "-p", "MainPID", "-p", "Job"]).stdout
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        require(props.get("LoadState") == "loaded" and props.get("ActiveState") in {"inactive", "failed"})
        require(props.get("MainPID") == "0" and props.get("Job") in {"", "0"})
        exists = run(["podman", "container", "exists", name], check=False)
        require(exists.returncode in (0, 1))
        if exists.returncode == 0:
            state = json.loads(run(["podman", "inspect", "--format", "{{json .State}}", name]).stdout)
            require(state.get("Running") is False and state.get("Pid") == 0)


def mutation_sql(state):
    # The ORM stores hex IDs as BYTEA and task prompts as compressed BYTEA.
    # Decode every ID predicate; mutate the ordinary text name, never the prompt.
    # Every interpolated value is constrained to a 32-hex ID or its fixed prefix.
    validate_state(state)
    sid, task, viewer = state["sessions"]["shared"], state["task"], state["viewer"]["id"]
    return f"""
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';
DO $legacy$
DECLARE n integer;
BEGIN
 UPDATE public.conversations SET title='legacy-mutated-after-checkpoint'
  WHERE workspace_id=0 AND id=decode('{sid}','hex') AND title='legacy-shared';
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>1 THEN RAISE EXCEPTION 'legacy-mutation-count'; END IF;
 UPDATE public.scheduled_tasks SET name='legacy-mutated-after-checkpoint'
  WHERE workspace_id=0 AND id=decode('{task}','hex') AND name='legacy-paused';
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>1 THEN RAISE EXCEPTION 'legacy-mutation-count'; END IF;
 DELETE FROM public.session_permissions
  WHERE workspace_id=0 AND conversation_id=decode('{sid}','hex') AND user_id='{viewer}' AND level=1;
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>1 THEN RAISE EXCEPTION 'legacy-mutation-count'; END IF;
END $legacy$;
COMMIT;
SELECT json_build_object('changed',
 (SELECT title='legacy-mutated-after-checkpoint' FROM public.conversations WHERE workspace_id=0 AND id=decode('{sid}','hex'))
 AND (SELECT name='legacy-mutated-after-checkpoint' FROM public.scheduled_tasks WHERE workspace_id=0 AND id=decode('{task}','hex'))
 AND NOT EXISTS (SELECT 1 FROM public.session_permissions WHERE workspace_id=0 AND conversation_id=decode('{sid}','hex') AND user_id='{viewer}'));
"""


def execute(operation, path, head, mcp):
    require((head, mcp) in {(OLD_HEAD, "absent"), (NEW_HEAD, "present")})
    guard()
    path = state_path(path)
    if operation == "seed":
        require(not path.exists())
        require((head, mcp) == (OLD_HEAD, "absent"))
        inspect_schema(head, mcp)
        state = new_state()
        state["snapshot"] = worker("seed", state)
        step("state")
        validate_state(state)
        save_new(path, state)
        verify_http(state)
    else:
        state = load_state(path)
        inspect_schema(head, mcp)
        if operation == "verify":
            actual = worker("verify", state)
            compare_snapshot(state["snapshot"], actual)
            verify_http(state)
        elif operation == "mutate":
            require_stopped_writers()
            step("mutate")
            require(psql(mutation_sql(state)) == {"changed": True})
            require_stopped_writers()
        else:
            require(operation == "inspect")


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise FixtureError()


def main(argv=None):
    step("arguments")
    parser = QuietParser(description=__doc__)
    parser.add_argument("operation", choices=("seed", "verify", "inspect", "mutate"))
    parser.add_argument("state")
    parser.add_argument("--expected-head", required=True, choices=(OLD_HEAD, NEW_HEAD))
    parser.add_argument("--mcp", required=True, choices=("absent", "present"))
    try:
        args = parser.parse_args(argv)
        execute(args.operation, args.state, args.expected_head, args.mcp)
    except Exception:
        print("FAIL legacy-" + current_step)
        return 1
    print("PASS legacy-" + args.operation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
