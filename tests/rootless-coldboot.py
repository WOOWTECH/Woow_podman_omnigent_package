#!/usr/bin/env python3
"""PR-only disposable KVM controller; never accepts a host or disk argument.

Raw output, keys, fixture tokens and disk images remain in an owned 0700 directory.
Only the fixed-schema receipt leaves it. No emulation fallback or owner SSH login.
"""
import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

GIB = 1024 ** 3
MIN_FREE = 12 * GIB + GIB // 2
DISK_GIB = 11
IMAGE_LIMIT = 800 * 1024 ** 2
REPO = Path(__file__).resolve().parent.parent
GUEST_SCRIPT = "/usr/local/lib/omnigent-coldboot-guest.py"
# The same binary/default machine and KVM/CPU/RAM settings serve probe and boot.
QEMU_MACHINE_ARGS = ("qemu-system-x86_64", "-accel", "kvm", "-cpu", "host",
                     "-smp", "2", "-m", "6144")
KVM_PROBE_STATUSES = frozenset({"passed", "api-rejected", "unknown-error", *errno.errorcode.values()})
GUEST_CODES = frozenset({"guest-guard", "guest-guard-runtime", "guest-guard-arguments",
                         "guest-guard-marker", "guest-guard-marker-nonce", "guest-guard-dmi",
                         "guest-guard-instance", "guest-guard-release", "guest-guard-owner",
                         "guest-guard-home", "guest-guard-owner-ssh", "guest-guard-controller",
                         "guest-guard-owner-locked", "guest-guard-sshd",
                         "freshness", "guest-packages", "source", "linger",
                         "install", "initial-health", "prepare-profiles", "initial-smoke",
                         "identity", "poweroff", "boot-identity", "manager-before-owner",
                         "health-before-owner", "timer", "retention", "runtime-receipt"})

INSTALL_FAILURE_CODES = frozenset({"install", "install-timeout", "install-runner-build", "install-render",
                                   "install-admin-bootstrap", "install-operator", "install-smoke", "install-unit-reload",
                                   "install-image-pull", "install-unit-start", "install-http-health",
                                   "install-postgres-health", "install-server-health"})
INSTALL_SIGNATURES = frozenset({"storage-full", "permission-denied", "dns", "registry-rate-limit",
                                "network-timeout", "multiple", "unclassified"})
GUEST_CODES = GUEST_CODES | INSTALL_FAILURE_CODES

GUEST_SSH_EMPTY_STATES = frozenset({"absent", "empty-directory", "empty-authorized-keys"})
GUEST_SSH_STATES = GUEST_SSH_EMPTY_STATES | {"unavailable", "unsafe-directory", "unexpected-entry",
                                          "unsafe-authorized-keys", "nonempty-authorized-keys"}


class Failure(Exception):
    pass


def require(ok):
    if not ok:
        raise Failure()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_contract(data):
    require(set(data) == {"url", "checksumsUrl", "sha256", "architecture", "release", "date"})
    require(re.fullmatch(r"[0-9]{8}", data["date"]) is not None)
    base = "https://cloud-images.ubuntu.com/noble/" + data["date"] + "/"
    require(data["url"] == base + "noble-server-cloudimg-amd64.img")
    require(data["checksumsUrl"] == base + "SHA256SUMS")
    require(data["architecture"] == "amd64" and data["release"] == "24.04")
    require(re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is not None)
    # Equality above also excludes credentials, fragments, alternate origins and redirects.
    require(urlsplit(data["url"]).netloc == "cloud-images.ubuntu.com")
    return data


def verify_image(pin, sums, path):
    entries = [line.split() for line in sums.splitlines() if line.strip()]
    matching = [row[0] for row in entries if len(row) == 2 and
                row[1].lstrip("*") == "noble-server-cloudimg-amd64.img"]
    require(matching == [pin["sha256"]])
    require(0 < path.stat().st_size <= IMAGE_LIMIT)
    require(digest(path) == pin["sha256"])


def validate_owned(path, parent, nonce, identity):
    require(path.parent == parent and path.name.startswith("omnigent-coldboot-"))
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not path.is_symlink())
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
    require((info.st_dev, info.st_ino) == identity)
    marker = path / "owner.json"
    require(not marker.is_symlink() and marker.is_file())
    require(json.loads(marker.read_text()) == {"nonce": nonce, "uid": os.getuid()})


def kvm_probe():
    # Called after a minimal per-user ACL, without requiring a new login/group session.
    with open("/dev/kvm", "r+b", buffering=0) as device:
        require(fcntl.ioctl(device, 0xAE00, 0) == 12)  # KVM_GET_API_VERSION
        vm_fd = fcntl.ioctl(device, 0xAE01, 0)       # KVM_CREATE_VM; no TCG fallback
        require(vm_fd >= 0)
        os.close(vm_fd)


def kvm_probe_status():
    try:
        kvm_probe()
        return "passed"
    except OSError as error:
        return errno.errorcode.get(error.errno, "unknown-error")
    except Failure:
        return "api-rejected"


def kvm_device_identity():
    info = Path("/dev/kvm").lstat()
    require(stat.S_ISCHR(info.st_mode))  # A symlink/replacement is not our device.
    # ACL masks can change mode bits; device identity and ownership must match.
    return info.st_dev, info.st_ino, info.st_rdev, info.st_uid, info.st_gid


def kvm_access_snapshot():
    info = Path("/dev/kvm").stat()
    identity = (info.st_dev, info.st_ino, info.st_rdev, info.st_mode, info.st_uid, info.st_gid)
    acl = subprocess.run(["getfacl", "-cpn", "/dev/kvm"], capture_output=True, check=True, timeout=5).stdout
    effective_access = os.access("/dev/kvm", os.R_OK | os.W_OK, effective_ids=True)
    return identity, acl, effective_access


def kvm_acl_change(before, after):
    # Only fixed categories leave the private snapshots, never ACL entries/UIDs.
    if before == after:
        return "unchanged"
    def entry(acl, prefix):
        return next((line.split(b"#", 1)[0].strip() for line in acl.splitlines()
                     if line.startswith(prefix)), None)
    user = f"user:{os.getuid()}:".encode("ascii")
    old_user, new_user = entry(before, user), entry(after, user)
    if old_user is not None and new_user is None:
        return "named-user-removed"
    if old_user != new_user:
        return "named-user-changed"
    if entry(before, b"mask::") != entry(after, b"mask::"):
        return "mask-changed"
    return "other-change"


def qemu_binary_sha256(binary):
    resolved = Path(binary).resolve(strict=True)
    info = resolved.stat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 128 * 1024 ** 2)
    return digest(resolved)


def kvm_apparmor_denial(log, pid):
    # No profile names, kernel output or process metadata leave this predicate.
    return any(all(part in line for part in (b'apparmor="DENIED"', b'operation="open"', b'name="/dev/kvm"'))
               and re.search(rb"\bpid=" + str(pid).encode("ascii") + rb"(?:\s|$)", line) is not None
               for line in log.splitlines())


def qemu_stderr_diagnostic(stderr):
    # Match only QEMU's own stderr, never serial/cloud-init/SSH output. Public
    # summaries are fixed strings: filenames, paths and arbitrary text stay private.
    cases = (
        (r"failed to find romfile|could not load PC BIOS", "qemu-missing-rom", "QEMU could not find a firmware ROM."),
        (r"Block node is read-only", "qemu-readonly-drive", "QEMU rejected a read-only block device."),
        (r"Could not open", "qemu-image-open", "QEMU could not open an image."),
        (r"Failed to get .*lock", "qemu-image-lock", "QEMU could not lock an image."),
        (r"failed to initialize kvm|Could not access KVM kernel module|failed to create kvm vm", "qemu-kvm-init", "QEMU could not initialize KVM."),
        (r"Cannot allocate memory|cannot set up guest memory", "qemu-memory", "QEMU could not allocate guest memory."),
        (r"Could not set up host forwarding rule", "qemu-host-forward", "QEMU could not bind the loopback SSH forward."),
        (r"Property .* not found|is not a valid device model name", "qemu-device-option", "QEMU rejected a device property or model."),
        (r"invalid accelerator|invalid option|invalid parameter", "qemu-option", "QEMU rejected a command option."),
        (r"error while loading shared libraries", "qemu-library", "QEMU could not load a runtime library."),
    )
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        for pattern, code, summary in cases:
            if re.search(pattern, line, re.IGNORECASE):
                return code, summary
    return "qemu-early-exit", "QEMU exited before SSH readiness; unrecognized stderr withheld."


def qemu_kvm_diagnostics(stderr):
    # QEMU 8.2 reports a failing operation before its final initialize error.
    # Match complete C-locale messages; publish only constants from these tables.
    operations = {
        "Could not access KVM kernel module": "open",
        "ioctl(KVM_CREATE_VM) failed": "create-vm",
        "failed to initialize kvm": "initialize",
        "Create kernel irqchip failed": "create-irqchip",
        "Enable kernel irqchip failed": "enable-irqchip",
    }
    errors = {
        "Operation not permitted": (1, "EPERM"),
        "No such file or directory": (2, "ENOENT"),
        "Input/output error": (5, "EIO"),
        "Cannot allocate memory": (12, "ENOMEM"),
        "Permission denied": (13, "EACCES"),
        "Device or resource busy": (16, "EBUSY"),
        "No such device": (19, "ENODEV"),
        "Invalid argument": (22, "EINVAL"),
        "Inappropriate ioctl for device": (25, "ENOTTY"),
        "No space left on device": (28, "ENOSPC"),
        "Function not implemented": (38, "ENOSYS"),
        "Operation not supported": (95, "EOPNOTSUPP"),
    }
    found = []
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        line = line.removeprefix("qemu-system-x86_64: ").removeprefix("-accel kvm: ")
        for message, operation in operations.items():
            prefix = message + ": "
            if not line.startswith(prefix):
                continue
            reason = "withheld"
            for description, (number, name) in errors.items():
                expected = f"{number} {description}" if operation == "create-vm" else description
                if line == prefix + expected:
                    reason = name
                    break
            item = {"operation": operation, "errno": reason}
            if item not in found and len(found) < 8:
                found.append(item)
    return found


def cloud_config(nonce, client_key, host_private, host_public, guest_script):
    return {
        "users": [
            {"name": "coldboot-controller", "uid": 1000, "shell": "/bin/bash",
             "lock_passwd": True, "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
             "ssh_authorized_keys": [client_key]},
            {"name": "coldboot-owner", "uid": 1100, "shell": "/usr/sbin/nologin",
             "lock_passwd": True}],
        "ssh_pwauth": False, "disable_root": True, "ssh_deletekeys": True,
        "ssh_keys": {"ed25519_private": host_private, "ed25519_public": host_public},
        "ssh_publish_hostkeys": {"enabled": False},
        "write_files": [
            {"path": "/var/lib/omnigent-coldboot.json", "owner": "root:root",
             "permissions": "0600", "content": json.dumps({"nonce": nonce})},
            {"path": GUEST_SCRIPT, "owner": "root:root", "permissions": "0700",
             "content": guest_script},
            {"path": "/etc/ssh/sshd_config.d/00-coldboot.conf", "owner": "root:root",
             "permissions": "0644", "content": "AllowUsers coldboot-controller\n"
             "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"}],
    }


class Controller:
    def __init__(self):
        self.stage = "host-preflight"
        self.work = None
        self.report_parent = None
        self.proc = None
        self.probe_proc = None
        self.kvm_access_before = None
        self.kvm_original_identity = None
        self.acl = None
        self.completed = []
        self.deadline = time.monotonic() + 36 * 60
        self.result = {"schemaVersion": 1, "status": "failed", "architecture": "linux/amd64",
                       "piState": "private", "coverage": "guest poweroff and new QEMU process",
                       "cleanupComplete": False, "completedStages": self.completed,
                       "exclusions": ["physical host cold boot", "host power-loss recovery", "arm64 guest",
                                      "shared Pi", "paid model execution"]}

    def run(self, args, timeout=60, data=None, check=True):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0)
        result = subprocess.run(args, input=data, capture_output=True, check=False,
                                timeout=min(timeout, remaining))
        if self.work:
            with (self.work / "commands.log").open("ab") as stream:
                stream.write(result.stdout + result.stderr)
        if check:
            require(result.returncode == 0)
        return result

    def stage_start(self, name):
        self.stage = name
        print("RUN coldboot " + name, flush=True)

    def done(self):
        self.completed.append(self.stage)
        print("PASS coldboot " + self.stage, flush=True)

    def preflight(self):
        self.stage_start("host-guard")
        require(os.environ.get("GITHUB_ACTIONS") == "true")
        require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted")
        require(os.environ.get("GITHUB_EVENT_NAME") == "pull_request")
        require(os.getuid() != 0 and platform.machine() == "x86_64")
        self.parent = Path(os.environ["RUNNER_TEMP"])
        require(self.parent.is_absolute() and self.parent.resolve() == self.parent)
        require(self.parent.is_dir() and self.parent.stat().st_uid == os.getuid())
        self.report_parent = self.parent
        self.done()
        self.stage_start("host-resources")
        require(shutil.disk_usage(self.parent).free >= MIN_FREE)
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        require(int(mem["MemAvailable"].split()[0]) * 1024 >= 7 * GIB)
        self.done()
        self.stage_start("package-source")
        self.head = os.environ["OMNIGENT_COLDBOOT_HEAD"]
        require(re.fullmatch(r"[0-9a-f]{40}", self.head) is not None)
        actual = self.run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.decode().strip()
        require(actual == self.head)
        self.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD"])
        self.pin = image_contract(json.loads((REPO / "tests/fixtures/coldboot-image.json").read_text()))
        self.nonce = str(uuid.uuid4())
        self.work = Path(tempfile.mkdtemp(prefix="omnigent-coldboot-", dir=self.parent))
        self.work.chmod(0o700)
        info = self.work.stat()
        self.identity = (info.st_dev, info.st_ino)
        (self.work / "owner.json").write_text(json.dumps({"nonce": self.nonce, "uid": os.getuid()}))
        self.result.update(packageCommit=actual, reviewedHead=self.head, ubuntuImage=self.pin,
                           guestDiskGiB=DISK_GIB, initialHostFreeBytes=shutil.disk_usage(self.parent).free)
        self.done()
        self.stage_start("kvm-device-access")
        self.kvm_original_identity = kvm_device_identity()
        self.acl = self.run(["getfacl", "-p", "/dev/kvm"]).stdout
        require(kvm_device_identity() == self.kvm_original_identity)
        self.run(["sudo", "-n", "setfacl", "-m", f"u:{os.getuid()}:rw", "/dev/kvm"])
        self.kvm_access_before = kvm_access_snapshot()
        self.done()
        self.stage_start("kvm-create-vm")
        kvm_probe()
        self.result["kvmCreateVmVerified"] = True
        self.done()
        self.probe_qemu_kvm()

    def refresh_kvm_access(self):
        validate_owned(self.work, self.parent, self.nonce, self.identity)
        require(self.acl is not None and self.kvm_access_before is not None)
        phase = self.stage
        self.stage = "kvm-access-settle"
        # CREATE_VM and close emit device-change events. Drain pending events
        # before the per-UID grant; never retry a failed QEMU or edit udev rules.
        self.run(["udevadm", "settle", "--timeout=5"], timeout=6)
        self.stage = "kvm-access-refresh"
        before = kvm_access_snapshot()
        # ACL masks may alter mode bits; inode/rdev/owner/group must not change.
        def device_key(snapshot):
            return snapshot[0][:3] + snapshot[0][4:]
        require(stat.S_ISCHR(before[0][3]))
        require(device_key(before) == device_key(self.kvm_access_before))
        record = {"phase": phase, "aclChange": kvm_acl_change(self.kvm_access_before[1], before[1]),
                  "effectiveAccessBefore": before[2], "effectiveAccessAfter": None}
        self.result.setdefault("kvmAccessRefreshes", []).append(record)
        self.run(["sudo", "-n", "setfacl", "-m", f"u:{os.getuid()}:rw", "/dev/kvm"], timeout=5)
        after = kvm_access_snapshot()
        record["effectiveAccessAfter"] = after[2]
        require(stat.S_ISCHR(after[0][3]) and device_key(after) == device_key(before) and after[2])
        # The differential baseline is the FINAL grant, not stale preflight access.
        self.kvm_access_before = after
        self.stage = phase

    def stop_qemu_probe(self):
        # This handle belongs only to the image-free probe, never the real guest.
        if self.probe_proc is None:
            return
        if self.probe_proc.poll() is None:
            self.probe_proc.terminate()
        try:
            self.probe_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.probe_proc.kill()
            self.probe_proc.wait(timeout=5)
        require(self.probe_proc.poll() is not None)
        self.probe_proc = None

    def probe_qemu_kvm(self):
        validate_owned(self.work, self.parent, self.nonce, self.identity)
        require(self.proc is None and self.probe_proc is None)
        require(self.deadline - time.monotonic() >= 3)
        self.stage_start("qemu-kvm-probe")
        # Paused CPU, no disks/default devices/network, before any image download.
        args = [*QEMU_MACHINE_ARGS, "-S", "-nodefaults", "-nic", "none",
                "-display", "none", "-monitor", "none", "-serial", "none"]
        try:
            with (self.work / "qemu-probe.stderr").open("wb") as output:
                self.refresh_kvm_access()
                self.probe_proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=output,
                                                   env={**os.environ, "LC_ALL": "C"})
            try:
                exit_code = self.probe_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                exit_code = self.probe_proc.poll()
            if exit_code is not None:
                self.record_qemu_exit(exit_code, probe=True)
                if {"operation": "open", "errno": "EACCES"} in self.result["qemuKvmDiagnostics"]:
                    self.diagnose_kvm_access()
                raise Failure()
        finally:
            self.stop_qemu_probe()
        self.result["qemuKvmProbeVerified"] = True
        self.done()

    def diagnose_kvm_access(self):
        # Failure-only differential; never changes ACLs, executes QEMU as root,
        # selects another accelerator, retries acceptance or turns a failure green.
        value = {"parentProbe": kvm_probe_status(), "childProbe": "unavailable",
                 "deviceUnchanged": None, "aclUnchanged": None,
                 "effectiveAccessBefore": self.kvm_access_before[2] if self.kvm_access_before else None,
                 "effectiveAccessAfter": None, "systemBinary": None,
                 "binarySha256": None, "kernelAudit": "unavailable"}
        self.result["qemuAccessDifferential"] = value
        try:
            code = "import runpy,sys; print(runpy.run_path(sys.argv[1])['kvm_probe_status']())"
            child = self.run([sys.executable, "-c", code, str(Path(__file__).resolve())],
                             timeout=5, check=False)
            status = child.stdout.decode("ascii").strip()
            if child.returncode == 0 and status in KVM_PROBE_STATUSES:
                value["childProbe"] = status
        except (Failure, OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            after = kvm_access_snapshot()
            value["effectiveAccessAfter"] = after[2]
            if self.kvm_access_before is not None:
                value["deviceUnchanged"] = after[0] == self.kvm_access_before[0]
                value["aclUnchanged"] = after[1] == self.kvm_access_before[1]
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            binary = shutil.which(QEMU_MACHINE_ARGS[0])
            if binary:
                value["systemBinary"] = os.path.samefile(binary, "/usr/bin/qemu-system-x86_64")
                value["binarySha256"] = qemu_binary_sha256(binary)
        except (Failure, OSError):
            pass
        try:
            audit = self.run(["sudo", "-n", "journalctl", "-k", "--no-pager", "-o", "cat",
                              "-n", "200", "--since=-1min"], timeout=5, check=False)
            if audit.returncode == 0:
                value["kernelAudit"] = ("apparmor-denied" if kvm_apparmor_denial(audit.stdout, self.probe_proc.pid)
                                        else "not-observed")
        except (Failure, OSError, subprocess.SubprocessError):
            pass

    def prepare(self):
        self.stage_start("verified-image-and-source")
        sums = self.run(["curl", "--fail", "--silent", "--show-error", "--proto", "=https",
                         "--max-time", "45", self.pin["checksumsUrl"]]).stdout.decode()
        self.base = self.work / "ubuntu.qcow2"
        # No -L: a redirect/body or obsolete daily image must fail its checksum.
        self.run(["curl", "--fail", "--silent", "--show-error", "--proto", "=https",
                  "--max-time", "240", "--max-filesize", str(IMAGE_LIMIT),
                  "--output", str(self.base), self.pin["url"]], timeout=250)
        verify_image(self.pin, sums, self.base)
        self.base.chmod(0o400)
        info = json.loads(self.run(["qemu-img", "info", "--output=json", str(self.base)]).stdout)
        require(info["format"] == "qcow2" and not info.get("backing-filename"))
        require(info["virtual-size"] < DISK_GIB * GIB and not info.get("encrypted", False))
        self.disk = self.work / "guest.qcow2"
        self.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(self.base),
                  str(self.disk), str(DISK_GIB) + "G"])
        self.disk_identity = (self.disk.stat().st_dev, self.disk.stat().st_ino)
        archive = self.work / "source.tar"
        self.run(["git", "-C", str(REPO), "archive", "--format=tar", "--output=" + str(archive), self.head])
        self.source_hash = digest(archive)
        self.result["sourceArchiveSha256"] = self.source_hash
        receipt = json.loads((REPO / "config/server-image.json").read_text())
        self.result.update(sourceCommit=receipt["sourceCommit"], imageDigest=receipt["imageDigest"])
        for name in ("client", "host"):
            self.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.work / name)])
        config = cloud_config(self.nonce, (self.work / "client.pub").read_text().strip(),
                              (self.work / "host").read_text(), (self.work / "host.pub").read_text().strip(),
                              (REPO / "tests/coldboot-guest.py").read_text())
        (self.work / "user-data").write_text("#cloud-config\n" + json.dumps(config))
        (self.work / "meta-data").write_text(json.dumps({"instance-id": self.nonce,
                                                       "local-hostname": "omnigent-coldboot"}))
        self.run(["cloud-localds", str(self.work / "seed.img"), str(self.work / "user-data"),
                  str(self.work / "meta-data")])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        (self.work / "known_hosts").write_text(
            f"[127.0.0.1]:{self.port} " + (self.work / "host.pub").read_text())
        self.done()

    def ssh(self, args, timeout=30, data=None, check=True):
        return self.run(["ssh", "-F", "/dev/null", "-p", str(self.port), "-i", str(self.work / "client"),
                         "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                         "-o", "UserKnownHostsFile=" + str(self.work / "known_hosts"),
                         "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519",
                         "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                         "-o", "ServerAliveCountMax=2", "coldboot-controller@127.0.0.1", shlex.join(args)],
                        timeout=timeout, data=data, check=check)

    def record_qemu_exit(self, exit_code, *, probe=False):
        try:
            with (self.work / ("qemu-probe.stderr" if probe else "qemu.stderr")).open("rb") as stream:
                stderr = stream.read(64 * 1024)
        except OSError:
            stderr = b""
        code, summary = qemu_stderr_diagnostic(stderr)
        self.result.update(qemuExitCode=exit_code, qemuFailureStage=self.stage,
                           qemuStderrSummary=summary,
                           qemuKvmDiagnostics=qemu_kvm_diagnostics(stderr))
        self.stage = code

    def boot(self):
        validate_owned(self.work, self.parent, self.nonce, self.identity)
        require(self.proc is None or self.proc.poll() == 0)
        require((self.disk.stat().st_dev, self.disk.stat().st_ino) == self.disk_identity)
        require(not self.disk.is_symlink() and not self.base.is_symlink())
        require(shutil.disk_usage(self.work).free >= GIB // 2)
        # New process on each invocation; never -snapshot, TCG, or an external disk.
        args = [*QEMU_MACHINE_ARGS,
                "-uuid", self.nonce, "-display", "none", "-monitor", "none", "-no-reboot",
                "-serial", "file:" + str(self.work / "serial.log"),
                "-drive", f"file={self.disk},format=qcow2,if=virtio",
                "-drive", f"file={self.work / 'seed.img'},format=raw,if=virtio,readonly=on",
                "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{self.port}-:22",
                # Disk boot uses the virtio NIC after Linux loads; no PXE ROM is needed.
                "-device", "virtio-net-pci,netdev=net0,romfile="]
        # Reset per-boot stderr so a previous boot's warning cannot mask this exit.
        # The guest serial stream remains a separate private file.
        with (self.work / "qemu.stderr").open("wb") as output:
            self.refresh_kvm_access()
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=output,
                                         env={**os.environ, "LC_ALL": "C"})
        deadline = min(time.monotonic() + 240, self.deadline)
        while time.monotonic() < deadline:
            exit_code = self.proc.poll()
            if exit_code is not None:
                self.record_qemu_exit(exit_code)
                raise Failure()
            require(shutil.disk_usage(self.work).free >= GIB // 2)
            if self.ssh(["true"], check=False).returncode == 0:
                self.ssh(["sudo", "-n", "cloud-init", "status", "--wait"], timeout=180)
                return
            time.sleep(3)
        raise Failure()

    def guest(self, phase, timeout=300):
        result = self.ssh(["sudo", "-n", "python3", GUEST_SCRIPT, phase, self.nonce,
                           self.head, self.source_hash], timeout=timeout, check=False)
        value = json.loads(result.stdout)
        ssh_state = value.get("ownerSshState")
        self.result["ownerSshState"] = ssh_state if isinstance(ssh_state, str) and ssh_state in GUEST_SSH_STATES else "unavailable"
        self.result["ownerSshIsolationVerified"] = (value.get("ownerSshIsolationVerified") is True
                                                   and self.result["ownerSshState"] in GUEST_SSH_EMPTY_STATES)
        self.result.pop("installerFailure", None)
        if result.returncode != 0 or value.get("ok") is not True:
            code = value.get("failureCode")
            if isinstance(code, str) and code in GUEST_CODES:
                self.stage = code
            diagnostic = value.get("installerFailure")
            if (isinstance(diagnostic, dict) and isinstance(code, str) and code in INSTALL_FAILURE_CODES
                    and diagnostic.get("failureCode") == code):
                exit_code, signature = diagnostic.get("exitCode"), diagnostic.get("signature")
                if ((exit_code is None and code == "install-timeout"
                     or type(exit_code) is int and -64 <= exit_code <= 255 and exit_code != 0)
                        and isinstance(signature, str) and signature in INSTALL_SIGNATURES):
                    self.result["installerFailure"] = {"failureCode": code, "exitCode": exit_code,
                                                       "signature": signature}
            raise Failure()
        require(self.result["ownerSshIsolationVerified"])
        return value

    def acceptance(self):
        self.preflight()
        self.prepare()
        self.stage_start("initial-guest-install")
        self.boot()
        with (self.work / "source.tar").open("rb") as source:
            self.ssh(["sh", "-c", "umask 077; cat > /home/coldboot-controller/source.tar"],
                     data=source.read(), timeout=90)
        initial = self.guest("prepare", timeout=1200)
        require(initial["packageCommit"] == self.head)
        self.done()
        self.stage_start("guest-poweroff")
        self.guest("poweroff")
        require(self.proc.wait(timeout=90) == 0)
        self.result["firstQemuExited"] = True
        self.done()
        self.stage_start("new-qemu-coldboot")
        self.boot()
        self.result["newQemuProcess"] = True
        self.done()
        self.stage_start("prelogin-retention")
        cold = self.guest("verify", timeout=420)
        required = ("bootIdChanged", "machineIdUnchanged", "ownerSessionsAbsent", "lingerEnabled",
                    "managerActiveBeforeOwnerProbe", "healthBeforeOwnerProbe", "timerActive",
                    "activeTokenRetained", "revokedTokenRejected", "dataAndSecretsRetained", "rootlessVerified")
        require(all(cold.get(key) is True for key in required))
        require(cold["packageCommit"] == self.head)
        self.result.update({key: cold[key] for key in required})
        self.result["bootIdentity"] = cold["bootIdentity"]
        self.done()
        self.stage_start("final-poweroff")
        self.guest("poweroff")
        require(self.proc.wait(timeout=90) == 0)
        self.done()

    def cleanup(self):
        # Best effort for each owned resource, but any failure prevents a passing receipt.
        failed = []
        try:
            self.stop_qemu_probe()
        except (Failure, OSError, subprocess.SubprocessError):
            failed.append("cleanup-qemu-probe")
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            failed.append("cleanup-qemu")
        try:
            if self.acl is not None:
                # Reaped VMs can have queued destroy events; settle before restore.
                subprocess.run(["udevadm", "settle", "--timeout=5"], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        except (OSError, subprocess.SubprocessError):
            failed.append("cleanup-kvm-settle")
        # After settle, revalidate the ORIGINAL device, even if refresh rejected
        # a replacement. Never write saved permissions onto a different device.
        can_restore_acl = False
        try:
            if self.acl is not None:
                require(self.kvm_original_identity is not None)
                require(kvm_device_identity() == self.kvm_original_identity)
                can_restore_acl = True
        except (Failure, OSError):
            failed.append("cleanup-kvm-identity")
        # A failed settle still attempts restoration if device identity matches.
        try:
            if can_restore_acl:
                subprocess.run(["sudo", "-n", "setfacl", "--restore=-"], input=self.acl, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            failed.append("cleanup-kvm-acl")
        try:
            if self.work is not None:
                require(self.proc is None or self.proc.poll() is not None)
                require(self.probe_proc is None or self.probe_proc.poll() is not None)
                validate_owned(self.work, self.parent, self.nonce, self.identity)
                shutil.rmtree(self.work)
                require(not self.work.exists())
        except (Failure, OSError, ValueError):
            failed.append("cleanup-files")
        if failed:
            self.result["cleanupFailureCodes"] = failed
            raise Failure()


def main():
    os.umask(0o077)
    controller = Controller()
    # Cancellation/overall timeout still reaches cleanup and never prints raw exceptions.
    def interrupted(_signum, _frame):
        raise Failure()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(37 * 60)
    ok = False
    try:
        controller.acceptance()
        ok = True
    except Exception:  # noqa: BLE001 -- only the fixed stage code may leave private logs
        controller.result["failureCode"] = controller.stage
    finally:
        signal.alarm(0)
        try:
            controller.cleanup()
            controller.result["cleanupComplete"] = True
        except Exception:  # noqa: BLE001 -- cleanup diagnostics are fixed codes too
            if ok:
                controller.stage = "cleanup"
            ok = False
        controller.result.update(status="passed" if ok else "failed",
                                 failureCode=None if ok else controller.stage)
        # RUNNER_TEMP is validated by preflight; never allow a user-chosen output path.
        if controller.report_parent is not None:
            report = controller.report_parent / "omnigent-coldboot-result.json"
            fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(controller.result, stream, indent=2)
                stream.write("\n")
    print(("PASS" if ok else "FAIL") + " guest coldboot " + controller.stage)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
