#!/usr/bin/env python3
"""Root controller inside one nonced disposable guest, never an existing host.

The package account has a nologin shell and no SSH key. setpriv does not use PAM or
create a login session. The second boot must pass root/system HTTP observations
before even this non-login account execution is allowed.
"""
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

ROOT = Path("/var/lib/omnigent-coldboot")
MARKER = Path("/var/lib/omnigent-coldboot.json")
SOURCE = Path("/opt/omnigent-coldboot-source")
HOME = Path("/home/coldboot-owner")
STATE = HOME / ".coldboot/profiles.json"
UID = 1100
OWNER = "coldboot-owner"
UNITS = ("omnigent-postgres.service", "omnigent-server.service", "omnigent-runner.service",
         "omnigent-server-health.timer")
phase = "guest-guard"
owner_ssh_observation = "unavailable"
installer_observation = None


class Failure(Exception):
    pass


def require(ok):
    if not ok:
        raise Failure()


def stage(name):
    global phase
    phase = name


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installer_failure(output, returncode):
    # Match only known fatal messages from the reviewed installer/library. Never
    # return a substring, command, URL, path or exception from private output.
    code = "install"
    fatal = {
        "podman build failed; nothing was changed": "install-runner-build",
        "the rendered units failed the dry-run; nothing was installed": "install-render",
        "server still needs setup; check the INIT_ADMIN secret and server log": "install-admin-bootstrap",
        "operator provisioning failed": "install-operator",
        "tests/smoke.sh failed; see the output above": "install-smoke",
        "systemctl --user daemon-reload failed": "install-unit-reload",
    }
    for line in output.decode(errors="replace").splitlines():
        if not line.startswith("install.sh: ERROR: "):
            continue
        message = line.removeprefix("install.sh: ERROR: ")
        code = fatal.get(message, "install")
        if message.startswith("podman pull ") and message.endswith(" failed; nothing was changed"):
            code = "install-image-pull"
        elif message.startswith(("start failed; see: journalctl --user -u ",
                                 "restart failed; see: journalctl --user -u ")):
            code = "install-unit-start"
        elif message == "http://127.0.0.1:18000/health did not answer 200; see: journalctl --user -u omnigent-server -n 100":
            code = "install-http-health"
        elif message == "omnigent-postgres did not become healthy; see: journalctl --user -u omnigent-postgres -n 100":
            code = "install-postgres-health"
        elif message == "omnigent-server did not become healthy; see: journalctl --user -u omnigent-server -n 100":
            code = "install-server-health"
    # These are observed error signatures, not a root-cause claim. Multiple
    # signatures remain ambiguous; publish only a fixed vocabulary.
    signatures = {"storage-full": (b"no space left on device",),
                  "permission-denied": (b"permission denied",),
                  "dns": (b"temporary failure resolving", b"temporary failure in name resolution", b"could not resolve host"),
                  "registry-rate-limit": (b"toomanyrequests",),
                  "network-timeout": (b"tls handshake timeout", b"i/o timeout",)}
    lowered = output.lower()
    matches = [name for name, needles in signatures.items() if any(needle in lowered for needle in needles)]
    return {"failureCode": code, "exitCode": returncode,
            "signature": matches[0] if len(matches) == 1 else "multiple" if matches else "unclassified"}


def run(args, timeout=60, data=None, cwd=None):
    global installer_observation
    try:
        result = subprocess.run(args, input=data, capture_output=True, check=False, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as error:
        if phase == "install":
            installer_observation = installer_failure((error.stdout or b"") + b"\n" + (error.stderr or b""), None)
            installer_observation["failureCode"] = "install-timeout"
            stage("install-timeout")
        raise
    if ROOT.is_dir():
        with (ROOT / "private.log").open("ab") as output:
            output.write(result.stdout + result.stderr)
    if phase == "install" and result.returncode != 0:
        installer_observation = installer_failure(result.stdout + b"\n" + result.stderr, result.returncode)
        stage(installer_observation["failureCode"])
    require(result.returncode == 0)
    return result.stdout.decode().strip()


def root_file(path):
    info = path.lstat()
    require(not path.is_symlink() and stat.S_ISREG(info.st_mode))
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600)


def guest_identity_matches(nonce, marker, dmi_uuid, instance_id):
    stage("guest-guard-marker-nonce")
    require(marker == {"nonce": nonce})
    stage("guest-guard-dmi")
    require(dmi_uuid.strip().lower() == nonce)
    stage("guest-guard-instance")
    require(instance_id.strip() == nonce)


def inspect_owner_ssh():
    path = HOME / ".ssh"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != UID or stat.S_IMODE(info.st_mode) != 0o700:
        return "unsafe-directory"
    entries = {entry.name for entry in path.iterdir()}
    if not entries:
        return "empty-directory"
    if entries != {"authorized_keys"}:
        return "unexpected-entry"
    info = (path / "authorized_keys").lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != UID
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        return "unsafe-authorized-keys"
    return "empty-authorized-keys" if info.st_size == 0 else "nonempty-authorized-keys"


def guard(nonce, head, source_hash):
    global owner_ssh_observation
    owner_ssh_observation = "unavailable"
    stage("guest-guard-runtime")
    require(os.geteuid() == 0 and platform.machine() == "x86_64")
    stage("guest-guard-arguments")
    require(str(uuid.UUID(nonce)) == nonce)
    require(re.fullmatch(r"[0-9a-f]{40}", head) is not None)
    require(re.fullmatch(r"[0-9a-f]{64}", source_hash) is not None)
    stage("guest-guard-marker")
    root_file(MARKER)
    marker = json.loads(MARKER.read_text())
    stage("guest-guard-dmi")
    dmi_uuid = Path("/sys/class/dmi/id/product_uuid").read_text()
    stage("guest-guard-instance")
    instance_id = Path("/var/lib/cloud/data/instance-id").read_text()
    guest_identity_matches(nonce, marker, dmi_uuid, instance_id)
    stage("guest-guard-release")
    require('VERSION_ID="24.04"' in Path("/etc/os-release").read_text())
    stage("guest-guard-owner")
    owner = pwd.getpwnam(OWNER)
    require(owner.pw_uid == UID and owner.pw_dir == str(HOME) and owner.pw_shell == "/usr/sbin/nologin")
    stage("guest-guard-home")
    require(HOME.is_dir() and not HOME.is_symlink() and HOME.stat().st_uid == UID)
    stage("guest-guard-owner-ssh")
    owner_ssh_observation = inspect_owner_ssh()
    require(owner_ssh_observation in {"absent", "empty-directory", "empty-authorized-keys"})
    stage("guest-guard-controller")
    require(pwd.getpwnam("coldboot-controller").pw_uid != UID)
    stage("guest-guard-owner-locked")
    require(run(["passwd", "--status", OWNER]).split()[:2] == [OWNER, "L"])
    stage("guest-guard-sshd")
    sshd = run(["/usr/sbin/sshd", "-T"]).splitlines()
    require([line for line in sshd if line.startswith("allowusers ")] == ["allowusers coldboot-controller"])
    require(all(line in sshd for line in ("passwordauthentication no", "kbdinteractiveauthentication no",
                                         "permitrootlogin no")))


def boot_identity():
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    machine = Path("/etc/machine-id").read_text().strip()
    require(str(uuid.UUID(boot)) == boot and re.fullmatch(r"[0-9a-f]{32}", machine) is not None)
    return {"boot": boot, "machine": machine}


def compare_boot(before, after):
    require(before["boot"] != after["boot"])
    require(before["machine"] == after["machine"])


def owner_command(args):
    # Deliberately no sudo -u, su, runuser, login, or SSH as the package owner.
    return ["setpriv", "--reuid=" + str(UID), "--regid=" + str(pwd.getpwnam(OWNER).pw_gid),
            "--init-groups", "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=" + str(HOME), "USER=" + OWNER, "LOGNAME=" + OWNER,
            "XDG_RUNTIME_DIR=/run/user/1100", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1100/bus",
            "OMNIGENT_ROOTLESS_PI_STATE=private", "LANG=C.UTF-8", *args]


def owner(args, timeout=60):
    return run(owner_command(args), timeout=timeout, cwd=HOME)


def health():
    with urlopen("http://127.0.0.1:18000/health", timeout=5) as response:
        require(response.status == 200 and json.load(response).get("status") == "ok")


def wait_health():
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            health()
            return
        except (Failure, OSError, ValueError):
            time.sleep(2)
    raise Failure()


def no_sessions_and_manager():
    # System manager queries as root cannot activate the package user's manager.
    require(run(["systemctl", "is-active", f"user@{UID}.service"]) == "active")
    require(run(["loginctl", "show-user", OWNER, "-p", "Linger", "--value"]) == "yes")
    require(run(["loginctl", "show-user", OWNER, "-p", "Sessions", "--value"]) == "")


def extract_source(archive, head, expected_hash):
    require(not SOURCE.exists() and not SOURCE.is_symlink())
    require(not archive.is_symlink() and archive.is_file() and archive.stat().st_size < 100 * 1024 ** 2)
    require(hash_file(archive) == expected_hash)
    require(run(["git", "get-tar-commit-id"], data=archive.read_bytes()) == head)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = Path(member.name)
            require(not path.is_absolute() and ".." not in path.parts)
            require(member.isfile() or member.isdir())
        SOURCE.mkdir(mode=0o755)
        SOURCE.chmod(0o755)
        source.extractall(SOURCE, filter="data")
    # The data filter leaves directory modes to umask; only this verified public
    # source tree needs traversal by the package owner. Preserve all file modes.
    for directory, _, _ in os.walk(SOURCE):
        Path(directory).chmod(0o755)
    require((SOURCE / "VERSION").read_text().strip() == "0.21.0-3")
    # The executed guest helper must be the same file in the reviewed archive.
    require(hash_file(SOURCE / "tests/coldboot-guest.py") == hash_file(Path(__file__)))


def save_context(value):
    path = ROOT / "context.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def load_context(head, source_hash):
    root_file(ROOT / "context.json")
    value = json.loads((ROOT / "context.json").read_text())
    require(value["head"] == head and value["sourceHash"] == source_hash)
    return value


def data_hashes():
    markers = (("omnigent-server", "/data/artifacts/rootless-ci-marker"),
               ("omnigent-runner", "/data/pi-agent/rootless-ci-marker"))
    return {container: hashlib.sha256(owner(["podman", "exec", container, "cat", path]).encode()).hexdigest()
            for container, path in markers}


def prepare(head, source_hash):
    stage("freshness")
    require(not ROOT.exists() and not ROOT.is_symlink() and not SOURCE.exists())
    for relative in (".config/omnigent", ".config/containers", ".config/systemd/user",
                     ".local/share/containers", ".local/state/woow-quadlet", "backups", ".coldboot"):
        require(not (HOME / relative).exists() and not (HOME / relative).is_symlink())
    require(not Path("/var/lib/systemd/linger/coldboot-owner").exists())
    require(not Path("/run/user/1100").exists())
    ROOT.mkdir(mode=0o700)
    ROOT.chmod(0o700)
    stage("guest-packages")
    run(["apt-get", "update", "-o", "Acquire::Retries=2", "-o", "Acquire::http::Timeout=30"], timeout=180)
    run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends",
         "podman", "uidmap", "slirp4netns", "fuse-overlayfs", "dbus-user-session", "curl", "jq", "git"], timeout=240)
    require(re.match(r"podman version 4\.9\.", run(["podman", "--version"])) is not None)
    run(["apt-get", "clean"])
    stage("source")
    extract_source(Path("/home/coldboot-controller/source.tar"), head, source_hash)
    stage("linger")
    for filename in ("/etc/subuid", "/etc/subgid"):
        require(any(line.startswith(OWNER + ":") for line in Path(filename).read_text().splitlines()))
    run(["loginctl", "enable-linger", OWNER])
    run(["systemctl", "start", f"user@{UID}.service"])
    no_sessions_and_manager()
    require(owner(["podman", "info", "--format", "{{.Host.Security.Rootless}}|{{.Host.CgroupsVersion}}"])
            == "true|v2")
    STATE.parent.mkdir(mode=0o700)
    STATE.parent.chmod(0o700)
    os.chown(STATE.parent, UID, pwd.getpwnam(OWNER).pw_gid)
    stage("install")
    owner(["bash", str(SOURCE / "scripts/install.sh"), "--rebuild", "--port", "18000", "--pi-state", "private",
           "--set", "OMNIGENT_ACCOUNTS_BASE_URL=http://127.0.0.1:18000",
           "--set", "OMNIGENT_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18000"], timeout=840)
    stage("initial-health")
    wait_health()
    stage("prepare-profiles")
    owner(["python3", str(SOURCE / "tests/rootless-mcp.py"), "prepare", str(STATE)], timeout=180)
    stage("initial-smoke")
    owner(["bash", str(SOURCE / "tests/smoke.sh")], timeout=240)
    owner(["bash", str(SOURCE / "tests/smoke-container.sh")], timeout=180)
    stage("identity")
    identity = boot_identity()
    save_context({"head": head, "sourceHash": source_hash, "identity": identity,
                  "fixtureHash": hash_file(STATE), "dataHashes": data_hashes(), "state": "prepared"})
    return {"ok": True, "packageCommit": head}


def verify(head, source_hash):
    stage("boot-identity")
    context = load_context(head, source_hash)
    require(context["state"] == "armed")
    after = boot_identity()
    compare_boot(context["identity"], after)
    # These gates MUST precede every owner command, including harmless podman info.
    stage("manager-before-owner")
    no_sessions_and_manager()
    stage("health-before-owner")
    wait_health()
    no_sessions_and_manager()
    # setpriv is non-login and cannot start user@1100 via PAM. Queries only below.
    stage("timer")
    for unit in UNITS:
        require(owner(["systemctl", "--user", "is-active", unit]) == "active")
    require(owner(["podman", "info", "--format", "{{.Host.Security.Rootless}}|{{.Host.CgroupsVersion}}"])
            == "true|v2")
    stage("retention")
    require(hash_file(STATE) == context["fixtureHash"])
    owner(["python3", str(SOURCE / "tests/rootless-mcp.py"), "check", str(STATE)], timeout=180)
    require(data_hashes() == context["dataHashes"])
    stage("runtime-receipt")
    owner(["python3", str(SOURCE / "scripts/verify-server-image.py"), "--container", "omnigent-server"], timeout=120)
    no_sessions_and_manager()
    context["state"] = "verified"
    save_context(context)
    return {"ok": True, "packageCommit": head, "bootIdChanged": True, "machineIdUnchanged": True,
            "bootIdentity": {"before": context["identity"]["boot"], "after": after["boot"],
                             "machineSha256": hashlib.sha256(after["machine"].encode()).hexdigest()},
            "ownerSessionsAbsent": True, "lingerEnabled": True, "managerActiveBeforeOwnerProbe": True,
            "healthBeforeOwnerProbe": True, "timerActive": True, "activeTokenRetained": True,
            "revokedTokenRejected": True, "dataAndSecretsRetained": True, "rootlessVerified": True}


def poweroff(head, source_hash):
    stage("poweroff")
    context = load_context(head, source_hash)
    require(context["state"] in ("prepared", "verified"))
    context["state"] = "armed" if context["state"] == "prepared" else "finished"
    save_context(context)
    run(["sync"])
    # Delay only to acknowledge over controller SSH; require QEMU exit on the host.
    run(["systemd-run", "--unit=omnigent-coldboot-poweroff", "--on-active=2s",
         "/usr/bin/systemctl", "poweroff"])
    return {"ok": True}


def main():
    global owner_ssh_observation, installer_observation
    owner_ssh_observation = "unavailable"
    installer_observation = None
    guard_passed = False
    os.umask(0o077)
    try:
        stage("guest-guard-arguments")
        require(len(sys.argv) == 5)
        action, nonce, head, source_hash = sys.argv[1:]
        require(action in ("prepare", "verify", "poweroff"))
        guard(nonce, head, source_hash)
        guard_passed = True
        value = {"prepare": prepare, "verify": verify, "poweroff": poweroff}[action](head, source_hash)
    except Exception:  # noqa: BLE001 -- public output must never contain credential-bearing errors
        value = {"ok": False, "failureCode": phase, "ownerSshState": owner_ssh_observation,
                 "ownerSshIsolationVerified": guard_passed}
        if installer_observation is not None:
            value["installerFailure"] = installer_observation
        print(json.dumps(value))
        return 1
    value.update(ownerSshState=owner_ssh_observation, ownerSshIsolationVerified=guard_passed)
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
