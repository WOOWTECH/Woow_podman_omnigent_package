#!/usr/bin/env python3
"""Private checkpoint metadata and lossless secret serialization. Never print secret values."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import quote

SECRET_NAMES = ("omnigent-postgres-password", "omnigent-admin-password", "omnigent-runner-password")
LEGACY_KEYS = dict(zip(("OMNIGENT_POSTGRES_PASSWORD", "OMNIGENT_ADMIN_PASSWORD", "OMNIGENT_RUNNER_PASSWORD"), SECRET_NAMES))
QUADLETS = ("omnigent.network", "omnigent-postgres.container", "omnigent-server.container", "omnigent-runner.container",
            "omnigent-postgres.volume", "omnigent-server.volume", "omnigent-pi.volume")
SYSTEMD = ("omnigent-server-health.service", "omnigent-server-health.timer")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def write_private(path, value):
    with path.open("wb") as output:
        os.chmod(path, 0o600)
        output.write(value if isinstance(value, bytes) else value.encode())


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def settings(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        key, sep, value = line.partition("=")
        require(sep and re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) and key not in result,
                "checkpoint env must contain unique plain KEY=VALUE lines")
        result[key] = value
    require(result.get("OMNIGENT_PI_STATE", "private") in ("private", "shared"), "invalid checkpoint Pi state")
    return result


def check_configuration(env, qdir):
    config = settings(env)
    server = (qdir / "omnigent-server.container").read_text().splitlines()
    runner = (qdir / "omnigent-runner.container").read_text().splitlines()
    def has(lines, wanted):
        require(wanted in lines, "checkpoint env differs from installed rendered units; reconcile the existing installation first")
    has(server, f"PublishPort={config.get('OMNIGENT_BIND')}:{config.get('OMNIGENT_PORT')}:8000")
    has(server, "Environment=OMNIGENT_ACCOUNTS_BASE_URL=" + config.get("OMNIGENT_ACCOUNTS_BASE_URL", ""))
    has(server, "Environment=OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME=" + config.get("OMNIGENT_ADMIN_USERNAME", "admin"))
    has(runner, "Environment=OMNIGENT_RUNNER_USERNAME=" + config.get("OMNIGENT_RUNNER_USERNAME", "operator"))
    mcp = [line for line in server if line.startswith("Environment=OMNIGENT_MCP_PUBLIC_BASE_URL=")]
    require(mcp == ["Environment=OMNIGENT_MCP_PUBLIC_BASE_URL=" + config.get("OMNIGENT_MCP_PUBLIC_BASE_URL", "")]
            or (not mcp and not config.get("OMNIGENT_MCP_PUBLIC_BASE_URL")), "checkpoint MCP URL differs from rendered units")
    mode = config.get("OMNIGENT_PI_STATE", "private")
    volume = "omnigent-pi.volume" if mode == "private" else "pi-agent-data.volume"
    has(runner, f"Volume={volume}:/data/pi-agent")
    require((qdir / "omnigent-pi.volume").exists() == (mode == "private"), "private Pi unit differs from configured Pi state")
    return mode


def unit_paths(qdir, sdir):
    paths = {"quadlet/" + n: qdir / n for n in QUADLETS}
    paths.update({"systemd/" + n: sdir / n for n in SYSTEMD})
    # Uncaptured overrides could change the effective image or volume. Refuse them instead
    # of claiming a complete checkpoint. The standard package installs no such overrides.
    for root in (qdir, sdir):
        require(not any(p.is_file() for directory in root.glob("omnigent*.d") for p in directory.rglob("*")),
                "package unit overrides need a separately verified checkpoint; refusing incomplete unit capture")
    return paths


def capture(args):
    mode = check_configuration(args.env, args.quadlet_dir)
    units = {}
    for name, path in unit_paths(args.quadlet_dir, args.systemd_dir).items():
        if name == "quadlet/omnigent-pi.volume" and mode == "shared":
            units[name] = None; continue
        require(path.is_file(), "required checkpoint unit is missing: " + name)
        dest = args.backup / "units" / name
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(dest, path.read_bytes()); units[name] = digest(dest)
    write_private(args.backup / "omnigent.env", args.env.read_bytes())
    write_private(args.backup / "checkpoint.json", json.dumps({"schemaVersion": 1, "piState": mode,
                  "envSha256": digest(args.env), "units": units}, indent=2) + "\n")


def check(args):
    receipt = read_json(args.backup / "checkpoint.json")
    require(receipt.get("schemaVersion") == 1, "unsupported checkpoint schema")
    saved_env = args.backup / "omnigent.env"
    require(digest(saved_env) == receipt.get("envSha256"), "checkpoint env checksum mismatch")
    require(digest(args.env) == receipt["envSha256"], "installed env differs from checkpoint; restore the saved env and matching old units before retrying")
    paths = unit_paths(args.quadlet_dir, args.systemd_dir)
    require(set(receipt.get("units", {})) == set(paths), "checkpoint unit inventory is incomplete")
    for name, current in paths.items():
        expected = receipt["units"][name]
        saved = args.backup / "units" / name
        if expected is None:
            require(name == "quadlet/omnigent-pi.volume" and receipt["piState"] == "shared", "invalid absent checkpoint unit")
            require(not current.exists() and not saved.exists(), "installed Pi units differ from checkpoint")
        else:
            require(saved.is_file() and digest(saved) == expected, "checkpoint unit checksum mismatch: " + name)
            require(current.is_file() and digest(current) == expected, "installed unit differs from checkpoint: " + name + "; restore matching old units first")
    mode = check_configuration(saved_env, args.backup / "units/quadlet")
    require(mode == receipt.get("piState"), "checkpoint Pi mode is inconsistent")
    print(mode)


def validate_secrets(values):
    require(isinstance(values, dict) and set(values) == set(SECRET_NAMES), "all three required secrets must be present exactly once")
    for value in values.values():
        require(isinstance(value, str) and value and "\x00" not in value, "secrets must be nonempty strings without NUL")
        value.encode("utf-8")
    return values


def save_secrets(args):
    values = {}
    for name in SECRET_NAMES:
        result = subprocess.run(["podman", "secret", "inspect", "--showsecret", "--format", "{{json .SecretData}}", name],
                                capture_output=True, text=True)
        require(result.returncode == 0, "cannot read required secret: " + name)
        values[name] = json.loads(result.stdout)
    validate_secrets(values)
    path = args.backup / "secrets.json"
    write_private(path, json.dumps({"schemaVersion": 1, "secrets": values}, ensure_ascii=True, indent=2) + "\n")
    write_private(path.with_suffix(".json.sha256"), digest(path) + "  secrets.json\n")


def prepare_secrets(args):
    path = args.backup / "secrets.json"
    if path.exists():
        expected = (args.backup / "secrets.json.sha256").read_text().split()
        require(len(expected) == 2 and expected[0] == digest(path) and expected[1] == "secrets.json", "secrets archive checksum mismatch")
        document = read_json(path)
        require(set(document) == {"schemaVersion", "secrets"} and document["schemaVersion"] == 1, "invalid secrets archive schema")
        values = validate_secrets(document["secrets"])
    else:
        # Historical backups were raw KEY=VALUE, not shell/env syntax. Keep quotes,
        # spaces and ' #' verbatim. Multiline/duplicate/incomplete legacy files fail.
        values = {}
        for line in (args.backup / "secrets.env").read_text().splitlines():
            key, sep, value = line.partition("=")
            require(sep and key in LEGACY_KEYS and LEGACY_KEYS[key] not in values, "malformed legacy secrets archive")
            values[LEGACY_KEYS[key]] = value
        validate_secrets(values)
    for name, value in values.items(): write_private(args.output / name, value)
    password = values["omnigent-postgres-password"]
    write_private(args.output / "omnigent-database-url", "postgresql+psycopg://omnigent:" + quote(password, safe="") + "@omnigent-postgres:5432/omnigent")
    # SQL goes to psql stdin, never argv or a shell expression. Standard strings plus
    # doubled quotes preserve backslashes, whitespace, unicode and embedded newlines.
    write_private(args.output / "restore-role.sql", "SET standard_conforming_strings = on;\nALTER ROLE omnigent PASSWORD '" + password.replace("'", "''") + "';\n")


def check_archive(args):
    with tarfile.open(args.archive, "r:*") as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as source:
                    while source.read(1024 * 1024): pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "check"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--backup", type=Path, required=True)
        cmd.add_argument("--quadlet-dir", type=Path, required=True)
        cmd.add_argument("--systemd-dir", type=Path, required=True)
        cmd.add_argument("--env", type=Path, required=True)
    cmd = commands.add_parser("save-secrets"); cmd.add_argument("--backup", type=Path, required=True)
    cmd = commands.add_parser("prepare-secrets")
    cmd.add_argument("--backup", type=Path, required=True); cmd.add_argument("--output", type=Path, required=True)
    cmd = commands.add_parser("check-archive"); cmd.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    try:
        {"capture": capture, "check": check, "save-secrets": save_secrets,
         "prepare-secrets": prepare_secrets, "check-archive": check_archive}[args.command](args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        # JSON parser exceptions may echo a secret-containing line; never print them.
        print("checkpoint validation failed for " + args.command + "; check required files, secrets, checksums and matching old units/config", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
