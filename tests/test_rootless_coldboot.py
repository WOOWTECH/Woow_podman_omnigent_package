"""Offline tests: no VM, download, KVM device, account or service is touched."""
import copy
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


host = module("coldboot_host", "rootless-coldboot.py")
guest = module("coldboot_guest", "coldboot-guest.py")
HEAD = "a" * 40
SOURCE_HASH = "b" * 64
NONCE = "0c439fed-dbd6-4a3e-9bc2-486d398cde63"


class PreflightTests(unittest.TestCase):
    def test_refuses_non_hosted_non_pr_before_any_command(self):
        for env in ({}, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"},
                    {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_EVENT_NAME": "workflow_dispatch"}):
            controller = host.Controller()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(controller, "run") as run, mock.patch("builtins.print"), self.assertRaises(host.Failure):
                controller.preflight()
            run.assert_not_called()
            self.assertIsNone(controller.work)

    def test_wrong_checkout_head_refuses_before_workdir_or_acl(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                   "GITHUB_EVENT_NAME": "pull_request", "RUNNER_TEMP": folder, "OMNIGENT_COLDBOOT_HEAD": HEAD}
            controller = host.Controller()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(host.platform, "machine", return_value="x86_64"), mock.patch.object(host.shutil, "disk_usage", return_value=mock.Mock(free=14 * host.GIB)), mock.patch.object(host.Path, "read_text", return_value="MemAvailable: 16777216 kB"), mock.patch.object(controller, "run", return_value=mock.Mock(stdout=b"d" * 40)) as run, mock.patch("builtins.print"), self.assertRaises(host.Failure):
                controller.preflight()
            self.assertEqual(len(run.call_args_list), 1)
            self.assertEqual(run.call_args.args[0][-2:], ["rev-parse", "HEAD"])
            self.assertIsNone(controller.work)
            self.assertIsNone(controller.acl)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.pin = json.loads((ROOT / "tests/fixtures/coldboot-image.json").read_text())

    def test_real_dated_pin(self):
        self.assertEqual(host.image_contract(self.pin), self.pin)
        self.assertEqual(self.pin["sha256"], "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94")
        self.assertEqual(self.pin["date"], "20261017")

    def test_reject_floating_foreign_or_credentialed_source(self):
        for field, value in (("url", self.pin["url"].replace("20261017", "current")),
                             ("url", self.pin["url"].replace("cloud-images.ubuntu.com", "example.org")),
                             ("url", self.pin["url"].replace("https://", "https://" + "user:secret" + "@")),
                             ("url", self.pin["url"] + "?x=1"),
                             ("checksumsUrl", "https://example.org/SHA256SUMS"),
                             ("sha256", "placeholder"), ("architecture", "arm64")):
            with self.subTest(field=field, value=value), self.assertRaises(host.Failure):
                host.image_contract({**self.pin, field: value})

    def test_hash_checks_bytes_and_manifest_not_just_url(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image"
            path.write_bytes(b"synthetic offline image")
            pin = {**self.pin, "sha256": host.digest(path)}
            sums = pin["sha256"] + " *noble-server-cloudimg-amd64.img\n"
            host.verify_image(pin, sums, path)
            for bad in ("", sums + sums, "0" * 64 + sums[64:]):
                with self.assertRaises(host.Failure):
                    host.verify_image(pin, bad, path)
            path.write_bytes(b"corrupted image")
            with self.assertRaises(host.Failure):
                host.verify_image(pin, sums, path)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        self.path = self.parent / "omnigent-coldboot-owned"
        self.path.mkdir(mode=0o700)
        self.path.chmod(0o700)
        self.marker = self.path / "owner.json"
        self.marker.write_text(json.dumps({"nonce": NONCE, "uid": os.getuid()}))
        self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self):
        host.validate_owned(self.path, self.parent, NONCE, self.identity)

    def test_owned_directory_accepted(self):
        self.validate()

    def test_wrong_nonce_inode_permissions_or_parent_rejected(self):
        self.marker.write_text(json.dumps({"nonce": "wrong", "uid": os.getuid()}))
        with self.assertRaises(host.Failure):
            self.validate()
        self.marker.write_text(json.dumps({"nonce": NONCE, "uid": os.getuid()}))
        with self.assertRaises(host.Failure):
            host.validate_owned(self.path, self.parent, NONCE, (0, 0))
        with self.assertRaises(host.Failure):
            host.validate_owned(self.path, self.parent.parent, NONCE, self.identity)
        self.path.chmod(0o755)
        with self.assertRaises(host.Failure):
            self.validate()

    def test_symlink_directory_and_marker_rejected(self):
        linked = self.parent / "omnigent-coldboot-link"
        linked.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(host.Failure):
            host.validate_owned(linked, self.parent, NONCE, self.identity)
        self.marker.unlink()
        self.marker.symlink_to(self.parent / "outside")
        with self.assertRaises(host.Failure):
            self.validate()

    def test_cleanup_refuses_replaced_marker_without_deleting(self):
        controller = host.Controller()
        controller.work, controller.parent = self.path, self.parent
        controller.nonce, controller.identity = NONCE, self.identity
        self.marker.write_text("{}")
        with self.assertRaises(host.Failure):
            controller.cleanup()
        self.assertTrue(self.path.exists())

    def test_cleanup_deletes_only_owned_directory(self):
        sentinel = self.parent / "unrelated"
        sentinel.write_text("keep")
        controller = host.Controller()
        controller.work, controller.parent = self.path, self.parent
        controller.nonce, controller.identity = NONCE, self.identity
        controller.cleanup()
        self.assertFalse(self.path.exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_acl_restore_failure_still_removes_owned_disk_and_fails(self):
        controller = host.Controller()
        controller.work, controller.parent = self.path, self.parent
        controller.nonce, controller.identity = NONCE, self.identity
        controller.acl = b"saved ACL"
        with mock.patch.object(host.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "setfacl")), self.assertRaises(host.Failure):
            controller.cleanup()
        self.assertFalse(self.path.exists())
        self.assertEqual(controller.result["cleanupFailureCodes"], ["cleanup-kvm-acl"])


class KvmAndTransportTests(unittest.TestCase):
    def test_real_create_vm_ioctl_required_and_fd_closed(self):
        with mock.patch("builtins.open", mock.mock_open()), mock.patch.object(host.fcntl, "ioctl", side_effect=[12, 81]) as ioctl, mock.patch.object(host.os, "close") as close:
            host.kvm_probe()
        self.assertEqual([call.args[1] for call in ioctl.call_args_list], [0xAE00, 0xAE01])
        close.assert_called_once_with(81)

    def test_missing_or_wrong_kvm_fails(self):
        with mock.patch("builtins.open", mock.mock_open()), mock.patch.object(host.fcntl, "ioctl", return_value=11), self.assertRaises(host.Failure):
            host.kvm_probe()
        with mock.patch("builtins.open", side_effect=PermissionError), self.assertRaises(PermissionError):
            host.kvm_probe()

    def test_current_user_acl_before_ioctl(self):
        source = (ROOT / "tests/rootless-coldboot.py").read_text()
        self.assertIn('f"u:{os.getuid()}:rw"', source)
        preflight = source[source.index("    def preflight"):source.index("    def prepare")]
        self.assertLess(preflight.index('"setfacl"'), preflight.index("kvm_probe()"))
        self.assertNotIn("chmod", preflight[preflight.index('"/dev/kvm"'):])

    def test_controller_only_and_hostkey_pinning(self):
        controller = host.Controller()
        controller.work, controller.port = Path("/private/owned"), 23456
        with mock.patch.object(controller, "run") as run:
            controller.ssh(["true"])
        args = run.call_args.args[0]
        self.assertIn("coldboot-controller@127.0.0.1", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("GlobalKnownHostsFile=/dev/null", args)
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", args)
        self.assertIn("UserKnownHostsFile=/private/owned/known_hosts", args)

    def test_cloud_init_no_owner_login_or_per_boot_service_start(self):
        config = host.cloud_config(NONCE, "client-public", "host-private", "host-public", "script")
        self.assertEqual(config["ssh_keys"]["ed25519_public"], "host-public")
        owner = config["users"][1]
        self.assertEqual(owner["shell"], "/usr/sbin/nologin")
        self.assertTrue(owner["lock_passwd"])
        self.assertNotIn("ssh_authorized_keys", owner)
        self.assertNotIn("runcmd", config)
        self.assertNotIn("bootcmd", config)
        self.assertFalse(config["ssh_pwauth"])
        self.assertIn("AllowUsers coldboot-controller", config["write_files"][2]["content"])


class BootDiagnosticsTests(unittest.TestCase):
    def test_qemu_probe_uses_boot_machine_without_images_or_network(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            probe = mock.Mock(poll=mock.Mock(side_effect=[None, None, 0]))
            probe.wait.side_effect = [subprocess.TimeoutExpired("qemu", 3), 0]
            with mock.patch.object(host.subprocess, "Popen", return_value=probe) as launch, mock.patch("builtins.print"):
                controller.probe_qemu_kvm()
            args = launch.call_args.args[0]
            self.assertEqual(args[:len(host.QEMU_MACHINE_ARGS)], list(host.QEMU_MACHINE_ARGS))
            for flag in ("-S", "-nodefaults"):
                self.assertIn(flag, args)
            self.assertEqual(args[args.index("-nic") + 1], "none")
            for flag in ("-drive", "-blockdev", "-netdev", "-kernel", "-initrd"):
                self.assertNotIn(flag, args)
            self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(launch.call_args.kwargs["stderr"].name.endswith("qemu-probe.stderr"))
            self.assertEqual(launch.call_args.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(probe.wait.call_args_list, [mock.call(timeout=3), mock.call(timeout=5)])
            probe.terminate.assert_called_once_with()
            probe.kill.assert_not_called()
            self.assertIsNone(controller.proc)
            self.assertIsNone(controller.probe_proc)
            self.assertTrue(controller.result["qemuKvmProbeVerified"])
            self.assertEqual(controller.result["status"], "failed")
            self.assertEqual(controller.completed, ["qemu-kvm-probe"])

    def test_qemu_probe_early_exit_reports_reason_before_download(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            probe = mock.Mock(poll=mock.Mock(return_value=1), wait=mock.Mock(return_value=1))
            def launch_probe(_args, **kwargs):
                kwargs["stderr"].write(b"Could not access KVM kernel module: Permission denied\nprivate-fixture-token\n")
                kwargs["stderr"].flush()
                return probe
            with mock.patch.object(host.subprocess, "Popen", side_effect=launch_probe), mock.patch.object(controller, "preflight", side_effect=controller.probe_qemu_kvm), mock.patch.object(controller, "diagnose_kvm_access") as diagnose, mock.patch.object(controller, "prepare") as prepare, mock.patch.object(controller, "boot") as boot, mock.patch("builtins.print"), self.assertRaises(host.Failure):
                controller.acceptance()
            diagnose.assert_called_once_with()
            prepare.assert_not_called()
            boot.assert_not_called()
            probe.terminate.assert_not_called()
            self.assertEqual(probe.wait.call_args_list, [mock.call(timeout=3), mock.call(timeout=5)])
            self.assertEqual(controller.result["qemuFailureStage"], "qemu-kvm-probe")
            self.assertEqual(controller.result["qemuKvmDiagnostics"], [{"operation": "open", "errno": "EACCES"}])
            self.assertEqual(controller.result["qemuExitCode"], 1)
            self.assertNotIn("private-fixture", json.dumps(controller.result))
            self.assertNotIn("qemuKvmProbeVerified", controller.result)
            self.assertIsNone(controller.proc)
            self.assertIsNone(controller.probe_proc)

    def test_qemu_probe_refuses_foreign_directory_and_existing_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            marker = controller.work / "owner.json"
            original = marker.read_text()
            marker.write_text("{}")
            with mock.patch.object(host.subprocess, "Popen") as launch, self.assertRaises(host.Failure):
                controller.probe_qemu_kvm()
            launch.assert_not_called()
            marker.write_text(original)
            for attr in ("proc", "probe_proc"):
                process = mock.Mock()
                setattr(controller, attr, process)
                with mock.patch.object(host.subprocess, "Popen") as launch, self.assertRaises(host.Failure):
                    controller.probe_qemu_kvm()
                launch.assert_not_called()
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                setattr(controller, attr, None)

    def test_qemu_probe_kills_only_owned_handle_when_termination_times_out(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            probe = mock.Mock(poll=mock.Mock(side_effect=[None, None, -9]))
            probe.wait.side_effect = [subprocess.TimeoutExpired("qemu", 3), subprocess.TimeoutExpired("qemu", 5), -9]
            with mock.patch.object(host.subprocess, "Popen", return_value=probe), mock.patch("builtins.print"):
                controller.probe_qemu_kvm()
            probe.terminate.assert_called_once_with()
            probe.kill.assert_called_once_with()
            self.assertEqual(probe.wait.call_args_list, [mock.call(timeout=3), mock.call(timeout=5), mock.call(timeout=5)])
            self.assertIsNone(controller.probe_proc)

    def test_qemu_probe_exit_at_alive_check_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            probe = mock.Mock(poll=mock.Mock(return_value=0))
            probe.wait.side_effect = [subprocess.TimeoutExpired("qemu", 3), 0]
            with mock.patch.object(host.subprocess, "Popen", return_value=probe), mock.patch("builtins.print"), self.assertRaises(host.Failure):
                controller.probe_qemu_kvm()
            self.assertEqual(controller.result["qemuExitCode"], 0)
            self.assertNotIn("qemuKvmProbeVerified", controller.result)
            self.assertEqual(controller.completed, [])
            probe.terminate.assert_not_called()

    def test_qemu_probe_stop_failure_keeps_handle_and_prevents_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            probe = mock.Mock(poll=mock.Mock(return_value=None))
            probe.wait.side_effect = subprocess.TimeoutExpired("qemu", 5)
            with mock.patch.object(host.subprocess, "Popen", return_value=probe), mock.patch("builtins.print"), self.assertRaises(subprocess.TimeoutExpired):
                controller.probe_qemu_kvm()
            self.assertIs(controller.probe_proc, probe)
            self.assertNotIn("qemuKvmProbeVerified", controller.result)
            self.assertEqual(controller.completed, [])
            self.assertIsNone(controller.proc)

    def test_live_probe_prevents_workdir_cleanup_and_success(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            controller.probe_proc = mock.Mock(poll=mock.Mock(return_value=None))
            controller.probe_proc.wait.side_effect = subprocess.TimeoutExpired("qemu", 5)
            with self.assertRaises(host.Failure):
                controller.cleanup()
            self.assertTrue(controller.work.exists())
            self.assertIn("cleanup-qemu-probe", controller.result["cleanupFailureCodes"])
            self.assertIn("cleanup-files", controller.result["cleanupFailureCodes"])
            self.assertNotIn("qemuKvmProbeVerified", controller.result)

    def test_probe_is_after_ioctl_and_before_image_preparation(self):
        source = (ROOT / "tests/rootless-coldboot.py").read_text()
        preflight = source[source.index("    def preflight"):source.index("    def prepare")]
        self.assertLess(preflight.index("kvm_probe()"), preflight.index("self.probe_qemu_kvm()"))
        acceptance = source[source.index("    def acceptance"):source.index("    def cleanup")]
        self.assertLess(acceptance.index("self.preflight()"), acceptance.index("self.prepare()"))

    def test_kvm_diagnostics_preserve_operation_and_errno_as_fixed_values(self):
        cases = (
            (b"Could not access KVM kernel module: Permission denied", "open", "EACCES"),
            (b"ioctl(KVM_CREATE_VM) failed: 16 Device or resource busy", "create-vm", "EBUSY"),
            (b"qemu-system-x86_64: failed to initialize kvm: Invalid argument", "initialize", "EINVAL"),
            (b"Create kernel irqchip failed: No space left on device", "create-irqchip", "ENOSPC"),
            (b"Enable kernel irqchip failed: Operation not permitted", "enable-irqchip", "EPERM"),
        )
        for line, operation, error in cases:
            with self.subTest(operation=operation):
                self.assertEqual(host.qemu_kvm_diagnostics(line), [{"operation": operation, "errno": error}])

    def test_kvm_diagnostics_unknown_text_never_escapes(self):
        for line in (
            b"Could not access KVM kernel module: private-fixture-token",
            b"qemu-system-x86_64: failed to initialize kvm: Permission denied private-fixture-token",
            b"private-fixture-token Permission denied",
            b"ioctl(KVM_CREATE_VM) failed: 99 Permission denied",
        ):
            with self.subTest(line=line):
                value = host.qemu_kvm_diagnostics(line)
                self.assertNotIn("private-fixture", json.dumps(value))
                self.assertNotIn("EACCES", json.dumps(value))

    def test_kvm_exit_receipt_retains_multiple_fixed_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            controller.stage = "initial-guest-install"
            (controller.work / "qemu.stderr").write_bytes(
                b"Could not access KVM kernel module: Permission denied\n"
                b"qemu-system-x86_64: failed to initialize kvm: Permission denied\n"
                b"private-fixture-token\n")
            controller.record_qemu_exit(1)
            self.assertEqual(controller.result["qemuKvmDiagnostics"], [
                {"operation": "open", "errno": "EACCES"},
                {"operation": "initialize", "errno": "EACCES"},
            ])
            self.assertEqual(controller.stage, "qemu-kvm-init")
            self.assertNotIn("private-fixture", json.dumps(controller.result))

    def controller(self, folder):
        controller = host.Controller()
        controller.parent = Path(folder)
        controller.work = controller.parent / "omnigent-coldboot-test"
        controller.work.mkdir(mode=0o700)
        controller.work.chmod(0o700)
        controller.nonce, controller.port = NONCE, 23456
        controller.identity = (controller.work.stat().st_dev, controller.work.stat().st_ino)
        (controller.work / "owner.json").write_text(json.dumps({"nonce": NONCE, "uid": os.getuid()}))
        controller.disk, controller.base = controller.work / "guest.qcow2", controller.work / "base.qcow2"
        controller.disk.touch()
        controller.base.touch()
        controller.disk_identity = (controller.disk.stat().st_dev, controller.disk.stat().st_ino)
        return controller

    def test_qemu_early_exit_reports_only_allowlisted_stderr_and_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            controller.stage = "initial-guest-install"
            (controller.work / "serial.log").write_text("private-cloud-init-fixture")
            def exited(_args, **kwargs):
                kwargs["stderr"].write(b"qemu-system-x86_64: failed to find romfile 'private-fixture.rom'\n")
                kwargs["stderr"].flush()
                return mock.Mock(poll=mock.Mock(return_value=1))
            with mock.patch.object(host.subprocess, "Popen", side_effect=exited) as launch, mock.patch.object(controller, "ssh") as ssh, self.assertRaises(host.Failure):
                controller.boot()
            args = launch.call_args.args[0]
            self.assertIn("virtio-net-pci,netdev=net0,romfile=", args)
            self.assertEqual(controller.stage, "qemu-missing-rom")
            self.assertEqual(controller.result["qemuExitCode"], 1)
            self.assertEqual(controller.result["qemuFailureStage"], "initial-guest-install")
            self.assertIn("ROM", controller.result["qemuStderrSummary"])
            self.assertNotIn("private-fixture", json.dumps(controller.result))
            self.assertNotIn("private-cloud-init-fixture", json.dumps(controller.result))
            ssh.assert_not_called()

    def test_unknown_qemu_stderr_cannot_become_public_output(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            def exited(_args, **kwargs):
                kwargs["stderr"].write(b"unrecognized private-fixture-token diagnostic\n")
                kwargs["stderr"].flush()
                return mock.Mock(poll=mock.Mock(return_value=-9))
            with mock.patch.object(host.subprocess, "Popen", side_effect=exited), self.assertRaises(host.Failure):
                controller.boot()
            self.assertEqual(controller.stage, "qemu-early-exit")
            self.assertEqual(controller.result["qemuExitCode"], -9)
            self.assertNotIn("private-fixture", json.dumps(controller.result))

    def test_boot_keeps_kvm_owned_disk_loopback_and_controller_ssh(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller(folder)
            with mock.patch.object(host.subprocess, "Popen", return_value=mock.Mock(poll=mock.Mock(return_value=None))) as launch, mock.patch.object(controller, "ssh", return_value=mock.Mock(returncode=0)) as ssh:
                controller.boot()
            args = launch.call_args.args[0]
            self.assertIn("virtio-net-pci,netdev=net0,romfile=", args)
            self.assertEqual(args[args.index("-accel") + 1], "kvm")
            self.assertEqual(args[:len(host.QEMU_MACHINE_ARGS)], list(host.QEMU_MACHINE_ARGS))
            self.assertNotIn("-S", args)
            self.assertNotIn("-nodefaults", args)
            self.assertIn(f"file={controller.disk},format=qcow2,if=virtio", args)
            self.assertIn("user,id=net0,hostfwd=tcp:127.0.0.1:23456-:22", args)
            self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(ssh.call_args_list[0].args[0], ["true"])
            self.assertEqual(ssh.call_args_list[1].args[0], ["sudo", "-n", "cloud-init", "status", "--wait"])


class KvmAccessDifferentialTests(unittest.TestCase):
    def test_snapshot_captures_effective_access_and_device_metadata(self):
        info = mock.Mock(st_dev=1, st_ino=2, st_rdev=3, st_mode=4, st_uid=5, st_gid=6)
        with mock.patch.object(host.Path, "stat", return_value=info), mock.patch.object(host.subprocess, "run", return_value=mock.Mock(stdout=b"acl")), mock.patch.object(host.os, "access", return_value=False) as access:
            self.assertEqual(host.kvm_access_snapshot(), ((1, 2, 3, 4, 5, 6), b"acl", False))
        access.assert_called_once_with("/dev/kvm", os.R_OK | os.W_OK, effective_ids=True)

    def test_binary_identity_hashes_resolved_file_and_refuses_nonregular_or_large(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "fixture-qemu"
            binary.write_bytes(b"synthetic executable bytes")
            link = Path(folder) / "symlink-qemu"
            link.symlink_to(binary)
            self.assertEqual(host.qemu_binary_sha256(link), host.digest(binary))
            with self.assertRaises(host.Failure):
                host.qemu_binary_sha256(Path(folder))
            with mock.patch.object(host.Path, "stat", return_value=mock.Mock(st_mode=0o100755, st_size=129 * 1024 ** 2)), self.assertRaises(host.Failure):
                host.qemu_binary_sha256(binary)

    def test_parent_probe_publishes_only_static_status(self):
        with mock.patch.object(host, "kvm_probe"):
            self.assertEqual(host.kvm_probe_status(), "passed")
        for error, expected in ((PermissionError(13, "private-fixture"), "EACCES"),
                                (OSError(16, "private-fixture"), "EBUSY"),
                                (host.Failure(), "api-rejected")):
            with mock.patch.object(host, "kvm_probe", side_effect=error):
                self.assertEqual(host.kvm_probe_status(), expected)

    def test_matching_audit_requires_owned_pid_exact_device_and_denial(self):
        line = b'apparmor="DENIED" operation="open" profile="private-fixture" name="/dev/kvm" pid=123 comm="qemu-system-x86"'
        self.assertTrue(host.kvm_apparmor_denial(line, 123))
        for changed in (line.replace(b"pid=123", b"pid=1234"),
                        line.replace(b"/dev/kvm", b"/dev/kvm-other"),
                        line.replace(b'"DENIED"', b'"ALLOWED"'),
                        line.replace(b'"open"', b'"file_mmap"')):
            self.assertFalse(host.kvm_apparmor_denial(changed, 123))

    def test_differential_retains_denial_without_permission_or_startup_changes(self):
        controller = host.Controller()
        controller.probe_proc = mock.Mock(pid=123)
        controller.kvm_access_before = ("device-identity", b"acl", True)
        controller.stage = "qemu-kvm-init"
        audit = b'apparmor="DENIED" operation="open" profile="private-fixture" name="/dev/kvm" pid=123'
        responses = [mock.Mock(returncode=0, stdout=b"passed\n"), mock.Mock(returncode=0, stdout=audit)]
        with mock.patch.object(host, "kvm_probe_status", return_value="passed"), mock.patch.object(host, "kvm_access_snapshot", return_value=controller.kvm_access_before), mock.patch.object(host, "qemu_binary_sha256", return_value=SOURCE_HASH), mock.patch.object(host.shutil, "which", return_value="/usr/bin/qemu-system-x86_64"), mock.patch.object(host.os.path, "samefile", return_value=True), mock.patch.object(controller, "run", side_effect=responses) as run:
            controller.diagnose_kvm_access()
        value = controller.result["qemuAccessDifferential"]
        self.assertEqual(value, {"parentProbe": "passed", "childProbe": "passed", "deviceUnchanged": True,
                                 "aclUnchanged": True, "effectiveAccessBefore": True, "effectiveAccessAfter": True,
                                 "systemBinary": True, "binarySha256": SOURCE_HASH, "kernelAudit": "apparmor-denied"})
        self.assertNotIn("private-fixture", json.dumps(controller.result))
        self.assertEqual(controller.stage, "qemu-kvm-init")
        self.assertEqual(controller.result["status"], "failed")
        self.assertNotIn("qemuKvmProbeVerified", controller.result)
        self.assertEqual(run.call_args_list[0].args[0][1], "-c")
        self.assertNotIn("sudo", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][:3], ["sudo", "-n", "journalctl"])
        self.assertTrue(all(call.kwargs["timeout"] <= 5 for call in run.call_args_list))

    def test_incomplete_diagnostic_is_withheld_and_cannot_mask_original_failure(self):
        controller = host.Controller()
        controller.probe_proc = mock.Mock(pid=123)
        controller.stage = "qemu-kvm-init"
        responses = [mock.Mock(returncode=0, stdout=b"private-fixture\n"), mock.Mock(returncode=1, stdout=b"private-fixture")]
        with mock.patch.object(host, "kvm_probe_status", return_value="EACCES"), mock.patch.object(host, "kvm_access_snapshot", side_effect=OSError), mock.patch.object(host.shutil, "which", return_value=None), mock.patch.object(controller, "run", side_effect=responses):
            controller.diagnose_kvm_access()
        value = controller.result["qemuAccessDifferential"]
        self.assertEqual(value["parentProbe"], "EACCES")
        self.assertEqual(value["childProbe"], "unavailable")
        self.assertEqual(value["kernelAudit"], "unavailable")
        self.assertIsNone(value["systemBinary"])
        self.assertIsNone(value["deviceUnchanged"])
        self.assertNotIn("private-fixture", json.dumps(value))
        self.assertEqual(controller.stage, "qemu-kvm-init")

    def test_changed_access_and_failed_child_cannot_look_successful(self):
        controller = host.Controller()
        controller.probe_proc = mock.Mock(pid=123)
        controller.kvm_access_before = ("old-device", b"old-acl", True)
        responses = [mock.Mock(returncode=1, stdout=b"passed\n"), mock.Mock(returncode=0, stdout=b"")]
        with mock.patch.object(host, "kvm_probe_status", return_value="EACCES"), mock.patch.object(host, "kvm_access_snapshot", return_value=("new-device", b"new-acl", False)), mock.patch.object(host, "qemu_binary_sha256", return_value=SOURCE_HASH), mock.patch.object(host.shutil, "which", return_value="/other/qemu"), mock.patch.object(host.os.path, "samefile", return_value=False), mock.patch.object(controller, "run", side_effect=responses):
            controller.diagnose_kvm_access()
        value = controller.result["qemuAccessDifferential"]
        self.assertFalse(value["deviceUnchanged"])
        self.assertFalse(value["aclUnchanged"])
        self.assertTrue(value["effectiveAccessBefore"])
        self.assertFalse(value["effectiveAccessAfter"])
        self.assertFalse(value["systemBinary"])
        self.assertEqual(value["childProbe"], "unavailable")
        self.assertEqual(value["kernelAudit"], "not-observed")


class GuestGateTests(unittest.TestCase):
    def setUp(self):
        self.before = {"boot": "11111111-1111-1111-1111-111111111111", "machine": "c" * 32}
        self.after = {"boot": "22222222-2222-2222-2222-222222222222", "machine": "c" * 32}
        self.context = {"state": "armed", "identity": self.before, "fixtureHash": SOURCE_HASH,
                        "head": HEAD, "sourceHash": SOURCE_HASH, "dataHashes": {"fixture": SOURCE_HASH}}

    def test_changed_boot_same_machine_required(self):
        guest.compare_boot(self.before, self.after)
        for after in (self.before, {**self.after, "machine": "d" * 32}):
            with self.assertRaises(guest.Failure):
                guest.compare_boot(self.before, after)

    def verify_with(self, *, context=None, after=None, manager_error=False, health_error=False):
        events = []
        def manager():
            events.append("root-manager")
            if manager_error:
                raise guest.Failure()
        def health():
            events.append("root-health")
            if health_error:
                raise guest.Failure()
        def owner(args, **kwargs):
            events.append("owner:" + args[0])
            return "true|v2" if args[0] == "podman" else "active"
        patches = [mock.patch.object(guest, "load_context", return_value=copy.deepcopy(context or self.context)),
                   mock.patch.object(guest, "boot_identity", return_value=after or self.after),
                   mock.patch.object(guest, "no_sessions_and_manager", side_effect=manager),
                   mock.patch.object(guest, "wait_health", side_effect=health),
                   mock.patch.object(guest, "owner", side_effect=owner),
                   mock.patch.object(guest, "hash_file", return_value=SOURCE_HASH),
                   mock.patch.object(guest, "data_hashes", return_value={"fixture": SOURCE_HASH}),
                   mock.patch.object(guest, "save_context")]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                value = guest.verify(HEAD, SOURCE_HASH)
            except guest.Failure:
                return None, events
        return value, events

    def test_no_owner_command_on_wrong_boot_machine_or_unarmed_context(self):
        for after, context in ((self.before, self.context),
                               ({**self.after, "machine": "d" * 32}, self.context),
                               (self.after, {**self.context, "state": "prepared"})):
            result, events = self.verify_with(after=after, context=context)
            self.assertIsNone(result)
            self.assertEqual(events, [])

    def test_no_owner_command_before_manager_and_health(self):
        for kwargs in ({"manager_error": True}, {"health_error": True}):
            result, events = self.verify_with(**kwargs)
            self.assertIsNone(result)
            self.assertFalse(any(event.startswith("owner:") for event in events))
        result, events = self.verify_with()
        self.assertTrue(result["activeTokenRetained"])
        self.assertEqual(events[:3], ["root-manager", "root-health", "root-manager"])
        self.assertTrue(events[3].startswith("owner:"))
        self.assertTrue(result["timerActive"])
        self.assertTrue(result["revokedTokenRejected"])

    def test_owner_uses_setpriv_without_pam_or_service_start(self):
        with mock.patch.object(guest.pwd, "getpwnam", return_value=mock.Mock(pw_gid=1100)):
            args = guest.owner_command(["systemctl", "--user", "is-active", "timer"])
        self.assertEqual(args[0], "setpriv")
        self.assertIn("--reuid=1100", args)
        self.assertFalse(set(args) & {"sudo", "su", "runuser", "login", "ssh", "start", "restart"})
        self.assertIn("HOME=/home/coldboot-owner", args)

    def test_root_manager_queries_are_read_only(self):
        with mock.patch.object(guest, "run", side_effect=["active", "yes", ""]) as run:
            guest.no_sessions_and_manager()
        self.assertEqual(run.call_args_list[0].args[0], ["systemctl", "is-active", "user@1100.service"])
        with mock.patch.object(guest, "run", side_effect=["active", "yes", "5"]), self.assertRaises(guest.Failure):
            guest.no_sessions_and_manager()

    def test_guest_nonce_guard_rejects_foreign_before_commands(self):
        with mock.patch.object(guest.os, "geteuid", return_value=1000), mock.patch.object(guest, "run") as run, self.assertRaises(guest.Failure):
            guest.guard(NONCE, HEAD, SOURCE_HASH)
        run.assert_not_called()

    def test_nonce_dmi_and_cloud_instance_must_all_match(self):
        guest.guest_identity_matches(NONCE, {"nonce": NONCE}, NONCE.upper(), NONCE + "\n")
        for marker, dmi, instance in (({}, NONCE, NONCE), ({"nonce": NONCE}, "foreign", NONCE),
                                      ({"nonce": NONCE}, NONCE, "foreign")):
            with self.assertRaises(guest.Failure):
                guest.guest_identity_matches(NONCE, marker, dmi, instance)

    def test_owner_command_runs_in_accessible_owner_home(self):
        with mock.patch.object(guest, "owner_command", return_value=["setpriv"]), mock.patch.object(guest, "run") as run:
            guest.owner(["podman", "info"])
        self.assertEqual(run.call_args.kwargs["cwd"], guest.HOME)

    def test_retention_checks_reuse_original_state_and_mcp_fixture(self):
        calls = []
        def owner(args, **kwargs):
            calls.append(args)
            return "true|v2" if args[0] == "podman" else "active"
        with mock.patch.object(guest, "load_context", return_value=copy.deepcopy(self.context)), mock.patch.object(guest, "boot_identity", return_value=self.after), mock.patch.object(guest, "no_sessions_and_manager"), mock.patch.object(guest, "wait_health"), mock.patch.object(guest, "hash_file", return_value=SOURCE_HASH), mock.patch.object(guest, "data_hashes", return_value={"fixture": SOURCE_HASH}), mock.patch.object(guest, "owner", side_effect=owner), mock.patch.object(guest, "save_context"):
            guest.verify(HEAD, SOURCE_HASH)
        self.assertIn(["python3", str(guest.SOURCE / "tests/rootless-mcp.py"), "check", str(guest.STATE)], calls)
        self.assertNotIn("prepare", [part for call in calls for part in call])
        self.assertFalse({"start", "restart", "enable-linger"} & {part for call in calls for part in call})


class SourceTests(unittest.TestCase):
    def archive(self, path, name="VERSION"):
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(name)
            contents = b"0.21.0-3\n"
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))

    def test_source_hash_and_git_commit_before_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "source.tar"
            target = Path(folder) / "extracted"
            self.archive(archive)
            with mock.patch.object(guest, "SOURCE", target), mock.patch.object(guest, "run", return_value=HEAD) as run:
                with self.assertRaises(guest.Failure):
                    guest.extract_source(archive, HEAD, "0" * 64)
                run.assert_not_called()
                self.assertFalse(target.exists())
            with mock.patch.object(guest, "SOURCE", target), mock.patch.object(guest, "run", return_value="d" * 40):
                with self.assertRaises(guest.Failure):
                    guest.extract_source(archive, HEAD, guest.hash_file(archive))
                self.assertFalse(target.exists())

    def test_tar_path_escape_and_links_rejected_before_extraction(self):
        for name in ("../escape", "/absolute"):
            with tempfile.TemporaryDirectory() as folder:
                archive = Path(folder) / "source.tar"
                target = Path(folder) / "extracted"
                self.archive(archive, name)
                with mock.patch.object(guest, "SOURCE", target), mock.patch.object(guest, "run", return_value=HEAD):
                    with self.assertRaises(guest.Failure):
                        guest.extract_source(archive, HEAD, guest.hash_file(archive))
                    self.assertFalse(target.exists())

    def test_tar_symlink_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "source.tar"
            target = Path(folder) / "extracted"
            with tarfile.open(archive, "w") as stream:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc"
                stream.addfile(info)
            with mock.patch.object(guest, "SOURCE", target), mock.patch.object(guest, "run", return_value=HEAD):
                with self.assertRaises(guest.Failure):
                    guest.extract_source(archive, HEAD, guest.hash_file(archive))
                self.assertFalse(target.exists())

    def test_valid_source_is_owner_readable_and_helper_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "source.tar"
            target = Path(folder) / "extracted"
            private_fixture = Path(folder) / "private-fixture"
            private_fixture.write_text("synthetic")
            private_fixture.chmod(0o600)
            files = (("VERSION", b"0.21.0-3\n", 0o644),
                     ("tests/coldboot-guest.py", Path(guest.__file__).read_bytes(), 0o644),
                     ("scripts/lib/helper.sh", b"#!/bin/sh\n", 0o755),
                     ("rootfs/usr/local/bin/helper", b"#!/bin/sh\n", 0o755))
            with tarfile.open(archive, "w") as stream:
                # Explicit archive directories and implicit parents both need traversal.
                for name in ("tests", "scripts", "scripts/lib"):
                    info = tarfile.TarInfo(name)
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    stream.addfile(info)
                for name, data, mode in files:
                    info = tarfile.TarInfo(name)
                    info.mode, info.size = mode, len(data)
                    stream.addfile(info, io.BytesIO(data))
            previous_umask = os.umask(0o077)
            try:
                with mock.patch.object(guest, "SOURCE", target), mock.patch.object(guest, "run", return_value=HEAD):
                    guest.extract_source(archive, HEAD, guest.hash_file(archive))
            finally:
                os.umask(previous_umask)
            for directory in (target, *(p for p in target.rglob("*") if p.is_dir())):
                self.assertEqual(directory.stat().st_mode & 0o7777, 0o755, str(directory))
            for name, data, mode in files:
                extracted = target / name
                self.assertEqual(extracted.stat().st_mode & 0o7777, mode, name)
                self.assertEqual(extracted.read_bytes(), data)
            self.assertEqual(private_fixture.stat().st_mode & 0o7777, 0o600)


class SequenceTests(unittest.TestCase):
    def test_poweroff_exit_before_new_qemu_and_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = host.Controller()
            controller.work, controller.head = Path(folder), HEAD
            (controller.work / "source.tar").write_bytes(b"test")
            events = []
            controller.proc = mock.Mock()
            controller.proc.wait.side_effect = lambda **kw: events.append("qemu-exited") or 0
            proofs = {key: True for key in ("bootIdChanged", "machineIdUnchanged", "ownerSessionsAbsent",
                      "lingerEnabled", "managerActiveBeforeOwnerProbe", "healthBeforeOwnerProbe", "timerActive",
                      "activeTokenRetained", "revokedTokenRejected", "dataAndSecretsRetained", "rootlessVerified")}
            def guest_call(phase, **kwargs):
                events.append(phase)
                return {"packageCommit": HEAD, "bootIdentity": {}, **proofs}
            with mock.patch.object(controller, "preflight"), mock.patch.object(controller, "prepare"), mock.patch.object(controller, "boot", side_effect=lambda: events.append("boot")), mock.patch.object(controller, "ssh"), mock.patch.object(controller, "guest", side_effect=guest_call), mock.patch("builtins.print"):
                controller.acceptance()
            self.assertEqual(events, ["boot", "prepare", "poweroff", "qemu-exited", "boot", "verify", "poweroff", "qemu-exited"])


class WorkflowTests(unittest.TestCase):
    def test_pr_only_exact_head_and_sanitized_artifact(self):
        source = (ROOT / ".github/workflows/rootless-coldboot.yml").read_text()
        self.assertIn("pull_request:", source)
        self.assertNotIn("workflow_dispatch:", source)
        self.assertNotIn("push:", source)
        self.assertIn("timeout-minutes: 45", source)
        self.assertIn("runs-on: ubuntu-24.04", source)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha }}", source)
        self.assertIn("persist-credentials: false", source)
        self.assertIn("contents: read", source)
        self.assertEqual(source.count("path:"), 1)
        self.assertIn("${{ runner.temp }}/omnigent-coldboot-result.json", source)

    def test_separate_harness_and_bounded_disk(self):
        source = (ROOT / "tests/rootless-coldboot.py").read_text()
        self.assertNotIn("rootless-acceptance.sh", source)
        self.assertNotIn('"tcg"', source)
        self.assertIn('"-accel", "kvm"', source)
        self.assertNotIn('"-snapshot"', source)
        self.assertIn("self.proc.wait(timeout=90)", source)
        self.assertLess(host.DISK_GIB * host.GIB + host.IMAGE_LIMIT + host.GIB // 2, host.MIN_FREE)
        self.assertLessEqual(host.MIN_FREE, 13 * host.GIB)
        self.assertIn("signal.alarm(37 * 60)", source)


if __name__ == "__main__":
    unittest.main()
