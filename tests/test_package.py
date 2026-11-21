"""Offline contract tests; all image receipts below are synthetic test fixtures."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify", REPO / "scripts/verify-server-image.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "quadlet").mkdir()
        for name in ("VERSION", "Containerfile.runner", "quadlet/omnigent-runner.container", "quadlet/omnigent-server.container"):
            shutil.copyfile(REPO / name, self.repo / name)
        self.receipt = {
            "schemaVersion": 1, "publishable": True, "component": "server", "variant": "server",
            "coreVersion": "0.21.0", "sourceCommit": "a" * 40,
            "wheelSha256": "b" * 64, "uiSha256": "c" * 64,
            "contentSha256": "1" * 64, "contentManifestSha256": "2" * 64,
            "image": "ghcr.io/woowtech/omnigent-server-kubernetes@sha256:" + "d" * 64,
            "imageDigest": "sha256:" + "d" * 64,
            "baseImage": "ghcr.io/omnigent-ai/omnigent-server:v0.21.0@sha256:" + "e" * 64,
            "architecture": ["linux/amd64", "linux/arm64"],
            "platforms": [{"architecture": "linux/" + arch, "image": "ghcr.io/woowtech/omnigent-server-kubernetes@sha256:" + digest * 64}
                          for arch, digest in (("amd64", "3"), ("arm64", "4"))],
            "tag": "ghcr.io/woowtech/omnigent-server-kubernetes:0.21.0-mcp.1-server-sha-" + "a" * 40 + "-run-123-1",
            "runId": "123", "runAttempt": "1",
            "ci": {"GITHUB_REPOSITORY": "WOOWTECH/omnigent", "GITHUB_SHA": "a" * 40,
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"},
        }
        self.pin = self.receipt["tag"] + "@" + self.receipt["imageDigest"]
        (self.repo / "quadlet/omnigent-server.container").write_text("Image=" + self.pin + "\n")
        self.info = {"Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40,
                                          "io.woowtech.omnigent.base-image": self.receipt["baseImage"]}},
                     "Architecture": "arm64", "RepoDigests": [self.receipt["image"]]}
        self.runtime = {"coreVersion": "0.21.0", "receipt": self.receipt}

    def test_runtime_probe_selects_generic_security_variant(self):
        import contextlib
        import io
        from unittest.mock import patch

        def generic_runtime(command, **kwargs):
            self.assertIn("--debian-security", command)
            self.assertIn("--image-variant", command,
                          "producer defaults to Kubernetes dependency policy")
            self.assertEqual(command[command.index("--image-variant") + 1], "server")
            return subprocess.CompletedProcess(command, 0)

        with patch("subprocess.run", side_effect=generic_runtime), \
             patch("importlib.metadata.version", return_value="0.21.0"), \
             patch("pathlib.Path.read_text", return_value=json.dumps(self.receipt)), \
             patch("sys.argv", ["probe", self.receipt["sourceCommit"]]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(verify.RUNTIME_PROBE, {})
        self.assertEqual(json.loads(output.getvalue())["coreVersion"], "0.21.0")

    def test_package_revision_is_independent_of_woow_image_tag(self):
        (self.repo / "VERSION").write_text("0.21.0-17\n")
        (self.repo / "quadlet/omnigent-runner.container").write_text("Image=localhost/woow-omnigent-runner:0.21.0-17\n")
        verify.check_receipt(self.repo, self.receipt)
        verify.check_runtime(self.receipt, self.info, self.runtime)

    def test_generic_variant_accepts_shared_public_package(self):
        verify.check_receipt(self.repo, self.receipt)
        verify.check_runtime(self.receipt, self.info, self.runtime)

    def test_generic_variant_is_independent_of_repository_name(self):
        receipt = copy.deepcopy(self.receipt)
        original = "ghcr.io/woowtech/omnigent-server-kubernetes"
        repository = "ghcr.io/woowtech/omnigent-server"
        for key in ("image", "tag"):
            receipt[key] = receipt[key].replace(original, repository)
        for platform in receipt["platforms"]:
            platform["image"] = platform["image"].replace(original, repository)
        (self.repo / "quadlet/omnigent-server.container").write_text(
            "Image=" + receipt["tag"] + "@" + receipt["imageDigest"] + "\n")
        verify.check_receipt(self.repo, receipt)

    def test_wrong_variant_tag_base_and_child_repository_are_rejected(self):
        for field in ("tag", "baseImage", "platforms"):
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.receipt)
                if field == "tag":
                    receipt[field] = receipt[field].replace("-server-sha-", "-server-kubernetes-sha-")
                elif field == "baseImage":
                    receipt[field] = receipt[field].replace("omnigent-server:", "omnigent-server-kubernetes:")
                else:
                    receipt[field][0]["image"] = receipt[field][0]["image"].replace("/omnigent-server-kubernetes@", "/other-package@")
                with self.assertRaises(ValueError):
                    verify.check_receipt(self.repo, receipt)

    def test_runtime_base_digest_must_match_receipt_exactly(self):
        info = copy.deepcopy(self.info)
        info["Config"]["Labels"]["io.woowtech.omnigent.base-image"] = self.receipt["baseImage"].replace("e" * 64, "f" * 64)
        with self.assertRaisesRegex(ValueError, "exact receipt baseImage"):
            verify.check_runtime(self.receipt, info, self.runtime)

    def test_engine_can_resolve_index_to_correct_architecture_child(self):
        self.info["RepoDigests"] = [self.receipt["platforms"][1]["image"]]
        verify.check_runtime(self.receipt, self.info, self.runtime)
        self.info["RepoDigests"] = [self.receipt["platforms"][0]["image"]]
        with self.assertRaisesRegex(ValueError, "pinned registry digest"):
            verify.check_runtime(self.receipt, self.info, self.runtime)

    def test_snapshot_and_kubernetes_receipts_are_rejected(self):
        for key, value in (("publishable", False), ("component", "server-kubernetes"), ("variant", "server-kubernetes")):
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.receipt)
                receipt[key] = value
                with self.assertRaises(ValueError):
                    verify.check_receipt(self.repo, receipt)

    def test_missing_and_wrong_provenance_are_rejected(self):
        for key in self.receipt:
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.receipt)
                del receipt[key]
                with self.assertRaises(ValueError):
                    verify.check_receipt(self.repo, receipt)

    def test_digest_drift_is_rejected(self):
        (self.repo / "quadlet/omnigent-server.container").write_text("Image=" + self.pin.replace("d" * 64, "f" * 64))
        with self.assertRaisesRegex(ValueError, "Quadlet"):
            verify.check_receipt(self.repo, self.receipt)

    def test_unchanged_tag_cannot_hide_different_runtime_source(self):
        for key in ("sourceCommit", "wheelSha256", "uiSha256"):
            with self.subTest(key=key):
                runtime = copy.deepcopy(self.runtime)
                runtime["receipt"][key] = "f" * len(self.receipt[key])
                with self.assertRaisesRegex(ValueError, "runtime receipt"):
                    verify.check_runtime(self.receipt, self.info, runtime)

    def test_actual_digest_label_architecture_and_version_are_checked(self):
        for key, value in (("RepoDigests", []), ("Architecture", "s390x"), ("Config", {"Labels": {}})):
            with self.subTest(key=key):
                info = copy.deepcopy(self.info)
                info[key] = value
                with self.assertRaises(ValueError):
                    verify.check_runtime(self.receipt, info, self.runtime)
        with self.assertRaisesRegex(ValueError, "installed core"):
            verify.check_runtime(self.receipt, self.info, {"coreVersion": "0.19.1"})


# Real package scripts execute against per-unit systemd states and lossless secret/storage
# doubles. This captures activating/auto-restart states and actual stdin/file bytes.
FAKE = r'''#!/usr/bin/env python3
import io, json, os, pathlib, sys, tarfile
args=sys.argv[1:]
root=pathlib.Path(os.environ["PACKAGE_TEST_STATE"])
name=pathlib.Path(sys.argv[0]).name
with (root/"calls").open("a") as out: out.write(name+" "+" ".join(args)+"\n")
states=json.loads((root/"states.json").read_text())
failure=os.environ.get("PACKAGE_TEST_FAIL", "")
def state(unit): return states.get(unit, {"ActiveState":"inactive", "SubState":"dead", "MainPID":"0", "ControlPID":"0", "Job":"0"})
def set_state(unit, active):
 states[unit]={"ActiveState":active,"SubState":"running" if active=="active" else "dead",
               "MainPID":"100" if active=="active" else "0","ControlPID":"0","Job":"0"}
 (root/"states.json").write_text(json.dumps(states))
if name == "systemctl":
 cmd=args[1]
 if cmd == "show":
  unit=args[-1]; props=[args[i+1] for i,x in enumerate(args) if x in ("-p", "--property")]
  for prop in props:
   value="loaded" if prop=="LoadState" else state(unit).get(prop, "")
   print(value if "--value" in args else prop+"="+value)
 elif cmd == "is-active": sys.exit(0 if state(args[-1])["ActiveState"] in ("active","reloading") else 3)
 elif cmd in ("stop", "start", "restart"):
  for unit in args[2:]:
   if cmd == "stop" and failure == "stop-stuck": continue
   if cmd == "start" and failure == "resume-failed": sys.exit(7)
   set_state(unit, "inactive" if cmd=="stop" else "active")
   if cmd == "stop" and failure in ("MainPID", "ControlPID", "Job"):
    states[unit][failure]="123"; (root/"states.json").write_text(json.dumps(states))
 sys.exit(0)
if args[:2] == ["container", "exists"]: sys.exit(0)
if args[0] == "inspect" or args[:2] == ["container", "inspect"]:
 unit=args[-1]+".service"
 if "Running" in " ".join(args): print("true" if state(unit)["MainPID"]!="0" or failure=="orphan-writer" else "false")
 elif "StartedAt" in " ".join(args): print("running|hc|healthy|2026-01-01T00:00:00Z")
 elif "Status" in " ".join(args): print("healthy" if "Health" in " ".join(args) else "running")
elif args[:2] == ["volume", "exists"]:
 sys.exit(1 if os.environ.get("PACKAGE_TEST_NO_PRIVATE") and args[-1]=="omnigent-pi-data" else 0)
elif args[:2] == ["volume", "export"]:
 if failure == "export": sys.exit(7)
 with tarfile.open(args[args.index("-o")+1], "w") as tf:
  data=b"synthetic artifact data"; entry=tarfile.TarInfo("artifacts/example"); entry.size=len(data)
  tf.addfile(entry, io.BytesIO(data))
elif args[:2] == ["volume", "import"]:
 with tarfile.open(args[-1]) as tf: tf.getmembers()
elif "pg_dump" in args:
 if failure == "dump": sys.exit(7)
 print("PGDMP synthetic fixture")
elif "pg_restore" in args:
 data=sys.stdin.read()
 if "--list" in args and not data.startswith("PGDMP"): sys.exit(7)
elif "psql" in args:
 with (root/"sql").open("a") as out: out.write(sys.stdin.read())
elif args[:2] == ["secret", "inspect"]:
 secrets=json.loads((root/"secrets-source.json").read_text())
 if args[-1] not in secrets: sys.exit(1)
 value=secrets[args[-1]]
 print(json.dumps(value) if "json" in " ".join(args) else value)
elif args[:2] == ["secret", "create"]:
 dest=root/"secrets-restored"; dest.mkdir(exist_ok=True)
 data=sys.stdin.buffer.read() if args[-1]=="-" else pathlib.Path(args[-1]).read_bytes()
 (dest/args[-2]).write_bytes(data)
'''


class LifecycleTests(unittest.TestCase):
    WRITERS = ("omnigent-runner.service", "omnigent-server.service")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        shutil.copytree(REPO / "scripts", self.repo / "scripts")
        shutil.copyfile(REPO / "VERSION", self.repo / "VERSION")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/smoke.sh").write_text('#!/bin/bash\nprintf "smoke\\n" >>"$PACKAGE_TEST_STATE/calls"\nsystemctl --user is-active --quiet omnigent-server-health.timer\n')
        (self.repo / "tests/smoke.sh").chmod(0o755)
        self.userdir = self.root / "user"
        self.config = self.userdir / ".config/omnigent/omnigent.env"
        self.config.parent.mkdir(parents=True)
        shutil.copyfile(REPO / "config/omnigent.env.example", self.config)
        self.config.chmod(0o600)
        self.qdir = self.userdir / ".config/containers/systemd"
        self.sdir = self.userdir / ".config/systemd/user"
        self.qdir.mkdir(parents=True); self.sdir.mkdir(parents=True)
        (self.root / "bin").mkdir()
        for name in ("podman", "systemctl"):
            path = self.root / "bin" / name
            path.write_text(FAKE); path.chmod(0o755)
        self.states = self.root / "states.json"
        self.states.write_text("{}")
        for unit in (*self.WRITERS, "omnigent-postgres.service", "omnigent-server-health.timer"):
            self.set_state(unit, "active")
        self.secrets = {name: "synthetic-password" for name in
                        ("omnigent-postgres-password", "omnigent-admin-password", "omnigent-runner-password")}
        self.write_secrets()
        self.env = dict(os.environ, HOME=str(self.userdir), PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                        PACKAGE_TEST_STATE=str(self.root), XDG_RUNTIME_DIR=str(self.root),
                        QL_STATE_ROOT=str(self.root / "state"), QL_QUADLET_DIR=str(self.qdir),
                        QL_SYSTEMD_USER_DIR=str(self.sdir))
        command = '. "$1/scripts/lib/quadlet-lib.sh"; . "$1/scripts/render-args.sh"; ql_env_load "$2"; render_args "$2"; ql_render "$1/quadlet" "$2" "$1/quadlet/render-vars" "$3" "${RENDER_ARGS[@]}"'
        subprocess.run(["bash", "-ec", command, "render", str(REPO), str(self.config), str(self.qdir)], env=self.env, check=True, capture_output=True)
        for name in ("omnigent-server-health.service", "omnigent-server-health.timer"):
            shutil.copyfile(REPO / "systemd" / name, self.sdir / name)
        (self.root / "calls").write_text("")

    def write_secrets(self):
        (self.root / "secrets-source.json").write_text(json.dumps(self.secrets))

    def set_state(self, unit, active, sub=None):
        states = json.loads(self.states.read_text())
        states[unit] = {"ActiveState": active, "SubState": sub or ("running" if active == "active" else "dead"),
                        "MainPID": "100" if active in ("active", "activating") else "0", "ControlPID": "0", "Job": "0"}
        self.states.write_text(json.dumps(states))

    def current_state(self, unit):
        return json.loads(self.states.read_text())[unit]["ActiveState"]

    def run_script(self, script, *args):
        return subprocess.run(["bash", str(self.repo / "scripts" / script), *args], env=self.env,
                              text=True, capture_output=True, timeout=20)

    def calls(self):
        return (self.root / "calls").read_text().splitlines()

    def clear_calls(self):
        (self.root / "calls").write_text("")

    def assert_no_mutations(self):
        for call in self.calls():
            self.assertFalse(any(marker in call for marker in
                (" --user stop ", " --user start ", " --user restart ", "volume rm", "volume import", "secret create", " psql ")), call)
            self.assertFalse("pg_restore" in call and "--list" not in call, call)

    def backup(self):
        result = self.run_script("backup.sh", "--stop", "--include-secrets", "--dest", str(self.root / "backups"))
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip())

    def restore(self, backup):
        return self.run_script("restore.sh", str(backup), "--with-secrets", "--yes")

    def checksum(self, path):
        import hashlib
        path.with_name(path.name + ".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest()+"  "+path.name+"\n")

    def switch_shared(self):
        self.config.write_text(self.config.read_text().replace("OMNIGENT_PI_STATE=private", "OMNIGENT_PI_STATE=shared"))
        p = self.qdir / "omnigent-runner.container"
        p.write_text(p.read_text().replace("Volume=omnigent-pi.volume:/data/pi-agent", "Volume=pi-agent-data.volume:/data/pi-agent"))
        (self.qdir / "omnigent-pi.volume").unlink(missing_ok=True)

    def test_missing_receipt_blocks_install_and_upgrade_before_host_commands(self):
        for script in ("install.sh", "upgrade.sh"):
            with self.subTest(script=script):
                result = self.run_script(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("receipt", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_activating_and_auto_restart_writers_are_stopped_before_dump(self):
        self.set_state(self.WRITERS[0], "activating", "start")
        self.set_state(self.WRITERS[1], "activating", "auto-restart")
        backup = self.backup()
        recorded = (backup / "writer-states.tsv").read_text()
        self.assertIn("omnigent-runner.service\tactivating\tstart", recorded)
        self.assertIn("omnigent-server.service\tactivating\tauto-restart", recorded)
        calls = self.calls()
        stop = next((i for i,c in enumerate(calls) if "systemctl --user stop" in c), None)
        self.assertIsNotNone(stop, "activating writers were never stopped")
        self.assertIn(" ".join(self.WRITERS), calls[stop])
        self.assertLess(stop, next(i for i,c in enumerate(calls) if "pg_dump" in c))
        for unit in self.WRITERS:
            self.assertEqual(self.current_state(unit), "active")

    def test_stop_must_confirm_no_active_unit_or_orphan_container(self):
        for failure in ("stop-stuck", "orphan-writer", "MainPID", "ControlPID", "Job"):
            with self.subTest(failure=failure):
                self.clear_calls(); self.env["PACKAGE_TEST_FAIL"] = failure
                result = self.run_script("backup.sh", "--stop", "--dest", str(self.root / "backups"))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("pg_dump" in c or "volume export" in c for c in self.calls()))

    def test_writers_stop_before_dump_and_start_after_exports(self):
        backup = self.backup(); calls = self.calls()
        index = lambda text: next(i for i,c in enumerate(calls) if text in c)
        self.assertLess(index("systemctl --user stop"), index("pg_dump"))
        self.assertLess(index("pg_dump"), index("volume export"))
        self.assertLess(max(i for i,c in enumerate(calls) if "volume export" in c), index("systemctl --user start"))
        self.assertFalse(any("pi-agent-data" in c for c in calls))
        self.assertEqual((backup / "secrets.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)

    def test_shared_mode_does_not_export_stale_private_pi_volume(self):
        self.switch_shared(); self.backup()
        self.assertFalse(any("omnigent-pi-data" in c or "pi-agent-data" in c for c in self.calls()))

    def test_failed_dump_or_export_restarts_original_writers(self):
        for stage in ("dump", "export"):
            with self.subTest(stage=stage):
                self.env["PACKAGE_TEST_FAIL"] = stage
                result = self.run_script("backup.sh", "--stop", "--dest", str(self.root / "backups"))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls()[-1], "systemctl --user start " + " ".join(self.WRITERS))
                for unit in self.WRITERS: self.assertEqual(self.current_state(unit), "active")

    def test_only_previously_running_or_starting_services_resume(self):
        self.set_state(self.WRITERS[0], "inactive")
        self.set_state(self.WRITERS[1], "activating", "auto-restart")
        self.backup()
        self.assertEqual(self.current_state(self.WRITERS[0]), "inactive")
        self.assertEqual(self.current_state(self.WRITERS[1]), "active")

    def test_previously_stopped_services_are_not_started_by_backup(self):
        for unit in self.WRITERS: self.set_state(unit, "inactive")
        self.backup()
        self.assertFalse(any("systemctl --user start" in c for c in self.calls()))
        self.assertTrue(any("systemctl --user stop" in c for c in self.calls()))

    def test_special_character_secrets_round_trip_exactly_and_stay_out_of_output(self):
        self.secrets = {name: "  synthetic # pass'word \\\"雪\nsecond line\n" for name in self.secrets}
        self.write_secrets(); backup = self.backup()
        result = self.restore(backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, value in self.secrets.items():
            self.assertEqual((self.root / "secrets-restored" / name).read_bytes(), value.encode())
            self.assertNotIn(value, result.stdout + result.stderr)
        from urllib.parse import quote
        self.assertEqual((self.root / "secrets-restored/omnigent-database-url").read_text(),
            "postgresql+psycopg://omnigent:"+quote(self.secrets["omnigent-postgres-password"], safe="")+"@omnigent-postgres:5432/omnigent")

    def test_legacy_secret_passphrase_with_hash_restores_losslessly(self):
        backup = self.backup(); (backup / "secrets.json").unlink(missing_ok=True)
        keys = ("OMNIGENT_POSTGRES_PASSWORD", "OMNIGENT_ADMIN_PASSWORD", "OMNIGENT_RUNNER_PASSWORD")
        (backup / "secrets.env").write_text("".join(key+"=synthetic # phrase\n" for key in keys))
        result = self.restore(backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "secrets-restored/omnigent-admin-password").read_text(), "synthetic # phrase")

    def test_invalid_or_missing_required_secrets_refuse_before_mutations(self):
        backup = self.backup()
        variants = ("not-json", "[]", '{"schemaVersion":1,"schemaVersion":1,"secrets":{}}', '{"schemaVersion":1,"secrets":{}}',
                    json.dumps({"schemaVersion":1,"secrets":{**self.secrets,"omnigent-admin-password":""}}),
                    json.dumps({"schemaVersion":1,"secrets":{**self.secrets,"omnigent-admin-password":"bad\u0000value"}}))
        for value in variants:
            with self.subTest(value=value):
                (backup / "secrets.json").write_text(value); self.checksum(backup / "secrets.json"); self.clear_calls()
                result = self.restore(backup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid or incomplete secrets", result.stderr)
                self.assert_no_mutations()

    def test_missing_secret_archive_refuses_before_mutations(self):
        backup = self.backup(); (backup / "secrets.json").unlink()
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid or incomplete secrets", result.stderr)
        self.assert_no_mutations()

    def test_malformed_legacy_secrets_refuse_before_mutations(self):
        backup = self.backup(); (backup / "secrets.json").unlink()
        key = "OMNIGENT_POSTGRES_PASSWORD"
        for value in (key + "=synthetic\n", "malformed\n",
                      key + "=one\n" + key + "=two\n"):
            with self.subTest(value=value):
                (backup / "secrets.env").write_text(value); self.clear_calls()
                result = self.restore(backup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid or incomplete secrets", result.stderr)
                self.assert_no_mutations()

    def test_backup_missing_secret_refuses_before_stopping(self):
        self.secrets.pop("omnigent-runner-password"); self.write_secrets()
        result = self.run_script("backup.sh", "--stop", "--include-secrets", "--dest", str(self.root / "backups"))
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()
        self.assertFalse(any("pg_dump" in c or "volume export" in c for c in self.calls()))

    def test_restore_rejects_corrupt_artifacts_before_stopping_or_dropping(self):
        backup = self.backup()
        next(backup.glob("omnigent-server-data-*.tar")).write_text("corrupt")
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assert_no_mutations()

    def test_well_checksummed_malformed_archive_refuses_before_mutations(self):
        backup = self.backup(); archive = next(backup.glob("omnigent-server-data-*.tar"))
        archive.write_bytes(b"not a tar archive"); self.checksum(archive)
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid volume archive", result.stderr)
        self.assert_no_mutations()

    def test_malformed_dump_refuses_before_mutations(self):
        backup = self.backup(); dump = next(backup.glob("*.dump"))
        dump.write_bytes(b"not a PostgreSQL dump"); self.checksum(dump)
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid PostgreSQL dump", result.stderr)
        self.assert_no_mutations()

    def test_missing_dump_checksum_refuses_before_mutations(self):
        backup = self.backup(); next(backup.glob("*.dump.sha256")).unlink()
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing checksum", result.stderr)
        self.assert_no_mutations()

    def test_missing_private_archive_refuses_before_mutations(self):
        backup = self.backup(); next(backup.glob("omnigent-pi-data-*.tar")).unlink()
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private checkpoint requires", result.stderr)
        self.assert_no_mutations()

    def test_missing_checkpoint_metadata_refuses_before_mutations(self):
        backup = self.backup(); (backup / "checkpoint.json").unlink()
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matching checkpoint units/config", result.stderr)
        self.assert_no_mutations()

    def test_changed_units_or_config_refuse_before_mutations(self):
        for name in ("omnigent-server.container", "omnigent-runner.container", "config"):
            with self.subTest(name=name):
                backup = self.backup()
                p = self.config if name == "config" else self.qdir / name
                before = p.read_text(); p.write_text(before + "\n# changed after checkpoint\n")
                self.clear_calls(); result = self.restore(backup)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_mutations(); p.write_text(before)

    def test_private_checkpoint_cannot_restore_onto_shared_units(self):
        backup = self.backup(); self.switch_shared()
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_shared_checkpoint_cannot_restore_onto_private_units(self):
        runner = (self.qdir / "omnigent-runner.container").read_bytes()
        private = (self.qdir / "omnigent-pi.volume").read_bytes(); config = self.config.read_bytes()
        self.switch_shared(); backup = self.backup()
        (self.qdir / "omnigent-runner.container").write_bytes(runner)
        (self.qdir / "omnigent-pi.volume").write_bytes(private); self.config.write_bytes(config)
        self.clear_calls(); result = self.restore(backup)
        self.assertNotEqual(result.returncode, 0); self.assert_no_mutations()

    def test_shared_restore_never_imports_private_or_shared_pi_volume(self):
        self.switch_shared(); backup = self.backup()
        self.clear_calls(); result = self.restore(backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("omnigent-pi-data" in c or "pi-agent-data" in c for c in self.calls()))

    def test_backup_reports_failed_resume(self):
        self.env["PACKAGE_TEST_FAIL"] = "resume-failed"
        result = self.run_script("backup.sh", "--stop", "--dest", str(self.root / "backups"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not resume", result.stderr)
        self.assertNotIn("backup complete", result.stderr)

    def test_restore_starts_health_timer_before_smoke_and_keeps_old_image_pins(self):
        pins = {name: (self.qdir / name).read_bytes() for name in ("omnigent-server.container", "omnigent-runner.container")}
        backup = self.backup(); self.set_state("omnigent-server-health.timer", "inactive")
        self.clear_calls(); result = self.restore(backup)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("systemctl --user start omnigent-server-health.timer"), calls.index("smoke"))
        for name, content in pins.items(): self.assertEqual((self.qdir / name).read_bytes(), content)

    def test_failed_upgrade_restores_units_and_keeps_writers_stopped(self):
        state = self.root / "state/omnigent"; state.mkdir(parents=True)
        unit = self.qdir / "omnigent-server.container"; before = unit.read_bytes()
        (state / "manifest").write_text(f"checksum  {unit}\n")
        installer = self.repo / "scripts/install.sh"
        installer.write_text('#!/bin/bash\nprintf "new unit\\n" >"$QL_QUADLET_DIR/omnigent-server.container"\nexit 1\n')
        installer.chmod(0o755)
        (self.repo / "scripts/verify-server-image.py").write_text("raise SystemExit(0)\n")
        result = self.run_script("upgrade.sh")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--with-secrets", result.stderr); self.assertEqual(unit.read_bytes(), before)
        for service in self.WRITERS: self.assertEqual(self.current_state(service), "inactive")


if __name__ == "__main__":
    unittest.main()
