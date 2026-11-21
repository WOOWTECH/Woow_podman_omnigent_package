"""Offline tests of the rootless acceptance fixture; these do not prove rootless operation."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REAL_RUN = subprocess.run
REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rootless", REPO / "tests/rootless-mcp.py")
rootless = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rootless)


def shell_function(name):
    source = (REPO / "tests/rootless-acceptance.sh").read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"


class RootlessFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private-state.json"
        self.rows = {}
        self.markers = {}
        self.count = 0
        self.cookie = "a" * 64
        self.peer = "synthetic-id|synthetic-start"
        mode = patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": "private"})
        mode.start(); self.addCleanup(mode.stop)
        for name, value in (("request", self.request), ("secret", lambda name: "synthetic-" + name),
                            ("subprocess.run", self.fake_run), ("subprocess.check_output", self.output)):
            target, attr = (rootless.subprocess, name.split(".")[1]) if name.startswith("subprocess.") else (rootless, name)
            p = patch.object(target, attr, side_effect=value)
            p.start(); self.addCleanup(p.stop)

    def request(self, url, method="GET", data=None, token=None):
        if url == rootless.BASE + "/auth/login": return 200, {"token": "synthetic-session"}
        if url == rootless.PROFILE_API:
            if method == "POST":
                self.count += 1
                row = {"id": str(self.count), "active": True}
                self.rows[row["id"]] = row
                return 201, {"profile": row, "connection_url": rootless.BASE + "/mcp/private_" + row["id"]}
            return 200, {"profiles": list(self.rows.values())}
        if url.startswith(rootless.PROFILE_API + "/"):
            self.rows[url.rsplit("/", 1)[1]]["active"] = False
            return 204, None
        row = self.rows.get(url.rsplit("_", 1)[1])
        if not row or not row["active"]: return 404, None
        if data["method"] == "initialize": return 200, {"result": {"protocolVersion": "2025-03-26"}}
        return 200, {"result": {"tools": [{"name": "omnigent_session_history"}] +
                                         [{"name": "synthetic-tool-" + str(i)} for i in range(14)]}}

    def fake_run(self, args, **kwargs):
        if args[:3] == ["podman", "exec", "omnigent-rootless-peer"]:
            self.markers[rootless.MARKERS[1][1]] = args[-1].encode()
        else:
            self.markers[args[-1]] = args[-2].encode()
        return subprocess.CompletedProcess(args, 0)

    def output(self, args, **kwargs):
        if args[:2] == ["podman", "inspect"]: return self.peer + "\n"
        if "sha256sum" in args: return self.cookie + "  cookie-path\n"
        return self.markers[args[-1]]

    def test_cookie_hash_uses_generic_entrypoint_artifact_directory(self):
        def generic_output(args, **kwargs):
            if args[-1] != "/data/artifacts/accounts-cookie-secret":
                raise subprocess.CalledProcessError(1, args)
            return self.cookie + "  private-cookie-path\n"
        with patch.object(rootless.subprocess, "check_output", side_effect=generic_output):
            self.assertEqual(rootless.cookie_hash(), self.cookie)

    def test_marker_write_creates_missing_parents_with_literal_values(self):
        real_run = REAL_RUN
        markers = (("fixture-server", str(Path(self.tmp.name) / "server/artifacts/marker")),
                   ("fixture-runner", str(Path(self.tmp.name) / "runner/pi-agent/marker")))
        # Execute the exact container shell command locally against fresh fixture paths.
        def container_shell(args, **kwargs):
            return real_run(args[3:], **kwargs)
        value = "checkpoint ' # $literal"
        with patch.object(rootless, "MARKERS", markers), \
             patch.object(rootless.subprocess, "run", side_effect=container_shell):
            rootless.marker_write(value)
        for _, path in markers:
            self.assertEqual(Path(path).read_text(), value)

    def test_failure_code_names_cookie_step_without_private_details(self):
        failure = Path(self.tmp.name) / "failure-code"
        private = "http://127.0.0.1:18000/mcp/private_synthetic-secret"
        with patch.object(rootless, "cookie_hash", side_effect=OSError(private)), \
             patch.dict(os.environ, {"OMNIGENT_ROOTLESS_FAILURE_FILE": str(failure)}), \
             patch("sys.argv", ["rootless-mcp", "prepare", str(self.path)]), \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(rootless.main(), 1)
        self.assertTrue(failure.is_file(), "safe failure code was not recorded")
        self.assertEqual(failure.read_text(), "cookie\n")
        self.assertEqual(failure.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(private, error.getvalue())

    def test_restart_and_restore_contract_with_post_checkpoint_mutation(self):
        rootless.prepare(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        rootless.check(self.path)
        baseline = copy.deepcopy((self.rows, self.markers))
        rootless.mutate(self.path)
        with self.assertRaisesRegex(ValueError, "initialization"):
            rootless.check(self.path)
        self.rows, self.markers = baseline
        rootless.check(self.path, restored=True)

    def test_revocation_must_survive_restart(self):
        rootless.prepare(self.path)
        state = json.loads(self.path.read_text())
        self.rows[state["revoked"]["id"]]["active"] = True
        with self.assertRaisesRegex(ValueError, "revoked profile"):
            rootless.check(self.path)

    def test_restored_database_must_discard_later_profiles(self):
        rootless.prepare(self.path)
        baseline = copy.deepcopy((self.rows, self.markers))
        rootless.mutate(self.path)
        state = json.loads(self.path.read_text())
        self.rows, self.markers = baseline
        self.rows[state["afterCheckpoint"]["id"]] = {"id": state["afterCheckpoint"]["id"], "active": True}
        with self.assertRaisesRegex(ValueError, "post-checkpoint"):
            rootless.check(self.path, restored=True)

    def test_both_data_volumes_are_checked(self):
        rootless.prepare(self.path)
        for _, marker in rootless.MARKERS:
            with self.subTest(marker=marker):
                self.markers[marker] = b"changed"
                with self.assertRaisesRegex(ValueError, "artifact checkpoint"):
                    rootless.check(self.path)
                self.markers[marker] = b"checkpoint"

    def test_cookie_secret_is_checked(self):
        rootless.prepare(self.path)
        self.cookie = "b" * 64
        with self.assertRaisesRegex(ValueError, "cookie secret"):
            rootless.check(self.path)

    def test_password_secrets_are_checked(self):
        rootless.prepare(self.path)
        with patch.object(rootless, "secret", return_value="different-synthetic-value"):
            with self.assertRaisesRegex(ValueError, "password secrets"):
                rootless.check(self.path)

    def test_error_output_does_not_expose_private_url(self):
        private = "http://127.0.0.1:18000/mcp/private_synthetic-secret"
        with patch.object(rootless, "prepare", side_effect=OSError(private)), \
             patch("sys.argv", ["rootless-mcp", "prepare", str(self.path)]), \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(rootless.main(), 1)
        self.assertNotIn(private, error.getvalue())
        self.assertIn("details withheld", error.getvalue())

    def test_shared_peer_interop_and_post_checkpoint_writes_survive_restore(self):
        with patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": "shared"}):
            rootless.checkpoint_seed(self.path)
            state = json.loads(self.path.read_text())
            rootless.storage_check(state)
            rootless.peer_write("external-after-checkpoint")
            rootless.checkpoint_restored(self.path)
            # A shared restore that rewinds another owner's write must fail.
            rootless.peer_write("checkpoint")
            with self.assertRaisesRegex(ValueError, "artifact checkpoint"):
                rootless.checkpoint_restored(self.path)
            rootless.peer_write("external-after-checkpoint")
            self.peer = "recreated-id|new-start"
            with self.assertRaisesRegex(ValueError, "peer was recreated"):
                rootless.checkpoint_restored(self.path)

    def test_shared_peer_must_read_runner_writes_before_seed_succeeds(self):
        original = self.output
        def isolated_peer(args, **kwargs):
            if args[:4] == ["podman", "exec", "omnigent-rootless-peer", "cat"]:
                return b"different-volume"
            return original(args, **kwargs)
        with patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": "shared"}), \
             patch.object(rootless.subprocess, "check_output", side_effect=isolated_peer):
            with self.assertRaisesRegex(ValueError, "shared filesystem"):
                rootless.checkpoint_seed(self.path)
        self.assertFalse(self.path.exists())

    def test_mutation_stops_at_writer_guard_and_changes_only_owned_storage(self):
        for mode in ("private", "shared"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": mode}):
                with patch.object(rootless, "stopped_writers", side_effect=ValueError("writer active")), \
                     patch.object(rootless.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "writer active"):
                        rootless.storage_mutate()
                    run.assert_not_called()
                events = []
                with patch.object(rootless, "stopped_writers", side_effect=lambda: events.append("guard")), \
                     patch.object(rootless.subprocess, "run", side_effect=lambda *a, **kw: events.append((a[0], kw))):
                    rootless.storage_mutate()
                self.assertEqual(events[0], "guard")
                self.assertEqual(events[-1], "guard")
                commands = [event[0] for event in events[1:-1]]
                mounts = [arg for cmd in commands for arg in cmd if ":/fixture" in arg]
                self.assertEqual(mounts, ["omnigent-server-data:/fixture"] +
                                 (["omnigent-pi-data:/fixture"] if mode == "private" else []))
                self.assertFalse(any("pi-agent-data:" in arg for cmd in commands for arg in cmd))
                peers = [cmd for cmd in commands if cmd[:3] == ["podman", "exec", "omnigent-rootless-peer"]]
                self.assertEqual(len(peers), int(mode == "shared"))
                if peers: self.assertEqual(peers[0][-1], "external-after-checkpoint")
                secrets = [(cmd, kw) for cmd, kw in events[1:-1] if cmd[:3] == ["podman", "secret", "create"]]
                self.assertEqual({cmd[-2] for cmd, _ in secrets}, set(rootless.SECRET_NAMES))
                for cmd, kw in secrets:
                    self.assertEqual(cmd[3], "--replace")
                    self.assertEqual(cmd[-1], "-")
                    self.assertEqual(len(kw["input"]), 48)
                    self.assertTrue(kw["check"])
                    self.assertNotIn(kw["input"].decode(), " ".join(cmd))

    def test_storage_shell_mutates_cookie_and_marker_losslessly(self):
        # Execute only the exact shell payload against a temporary fixture directory.
        directory = Path(self.tmp.name) / "fixture"
        (directory / "artifacts").mkdir(parents=True)
        def command(args, **kwargs):
            if args[:2] == ["podman", "run"]:
                i = args.index("-c")
                script = args[i + 1].replace("/fixture/", str(directory) + "/")
                return REAL_RUN(["sh", "-c", script, *args[i + 2:]], **kwargs)
            return subprocess.CompletedProcess(args, 0)
        value = "literal ' # $secret"
        with patch.object(rootless, "stopped_writers"), \
             patch.object(rootless.secrets, "token_hex", return_value=value), \
             patch.object(rootless.subprocess, "run", side_effect=command):
            rootless.storage_mutate()
        self.assertEqual((directory / "artifacts/accounts-cookie-secret").read_text(), value)
        self.assertEqual((directory / "artifacts/rootless-ci-marker").read_text(), "changed")
        self.assertEqual((directory / "rootless-ci-marker").read_text(), "changed")


class InventoryTests(unittest.TestCase):
    def make_checkpoint(self, directory, mode):
        path = Path(directory) / "checkpoint"
        path.mkdir()
        names = {"omnigent.env", "secrets.json", "secrets.json.sha256", "writer-states.tsv"}
        for data in ["omnigent-1.dump", "omnigent-server-data-1.tar"] + (
                ["omnigent-pi-data-1.tar"] if mode == "private" else []):
            names.update((data, data + ".sha256"))
        for name in names: (path / name).write_text("fixture")
        units = {"quadlet/omnigent-server.container": "digest", "quadlet/omnigent-pi.volume": None}
        (path / "units/quadlet").mkdir(parents=True)
        (path / "units/quadlet/omnigent-server.container").write_text("fixture")
        (path / "checkpoint.json").write_text(json.dumps({"piState": mode, "units": units}))
        return path

    def test_exact_private_and_shared_inventory(self):
        for mode in ("private", "shared"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": mode}):
                rootless.inventory(self.make_checkpoint(directory, mode))

    def test_inventory_rejects_external_extra_missing_duplicate_and_symlink_content(self):
        for bad in ("external-tar", "external-unit", "extra-unit", "missing", "duplicate", "extra-dir", "symlink", "wrong-mode"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"OMNIGENT_ROOTLESS_PI_STATE": "shared"}):
                path = self.make_checkpoint(directory, "shared")
                if bad == "external-tar": (path / "pi-agent-data-1.tar").touch()
                elif bad == "extra-unit": (path / "units/quadlet/peer.container").touch()
                elif bad == "missing": (path / "secrets.json").unlink()
                elif bad == "duplicate": (path / "omnigent-2.dump").touch()
                elif bad == "extra-dir": (path.parent / "other-checkpoint").mkdir()
                elif bad == "symlink":
                    (path / "omnigent.env").unlink()
                    (path / "omnigent.env").symlink_to("secrets.json")
                else:
                    metadata = json.loads((path / "checkpoint.json").read_text())
                    if bad == "wrong-mode": metadata["piState"] = "private"
                    else: metadata["units"]["quadlet/pi-agent-data.volume"] = None
                    (path / "checkpoint.json").write_text(json.dumps(metadata))
                with self.assertRaises(ValueError): rootless.inventory(path)


class HarnessGuardTests(unittest.TestCase):
    def test_refuses_non_disposable_host_before_tools(self):
        env = dict(os.environ)
        env.pop("GITHUB_ACTIONS", None)
        result = subprocess.run(["bash", str(REPO / "tests/rootless-acceptance.sh")], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires a disposable", result.stderr)

    def test_stage_does_not_hide_an_early_shell_function_failure(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        function = shell_function("run_stage")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "unexpected"
            command = ("set -e\nwork=$1\nOMNIGENT_ROOTLESS_FAILURE_FILE=$work/failure-code\ncompleted=()\nsystemctl() { :; }\n" + function +
                       "\nfail_early() { false; touch \"$work/unexpected\"; }\nrun_stage fixture fail_early\n")
            result = subprocess.run(["bash", "-c", command, "fixture", directory], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_checkpoint_mutation_waits_for_resumed_http_and_refuses_unready_server(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        wait = shell_function("wait_http")
        sequence = "run_stage backup backup_checkpoint" + source.split("run_stage backup backup_checkpoint", 1)[1].split("\nrun_stage maintenance", 1)[0]
        doubles = r'''run_stage() { shift; "$@"; }
die() { return 1; }
backup_checkpoint() {
  mkdir -p "$work/$1-backups/checkpoint"
  printf '%s\n' "$work/$1-backups/checkpoint" > "$work/$1-backup-path"
}
checkpoint_identity() { :; }
curl() {
  local count=0
  [[ ! -f $work/polls ]] || count=$(<"$work/polls")
  count=$((count + 1)); printf '%s' "$count" > "$work/polls"
  if [[ $fixture_ready == yes && $count -ge 2 ]]; then
    touch "$work/ready"; printf '{"status":"ok"}\n'; return 0
  fi
  return 7
}
jq() { cat >/dev/null; [[ -f $work/ready ]]; }
sleep() { :; }
python3() { [[ -f $work/ready ]] || return 7; touch "$work/mutated"; }
'''
        command = "set -euo pipefail\nwork=$1\nREPO=$2\nfixture_ready=$3\n" + doubles + wait + "\n" + sequence
        for ready in ("yes", "no"):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(["bash", "-c", command, "fixture", directory, str(REPO), ready],
                                        text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode == 0, ready == "yes", result.stderr)
                self.assertEqual((Path(directory) / "mutated").exists(), ready == "yes")
                if ready == "yes":
                    self.assertEqual((Path(directory) / "polls").read_text(), "2")

    def test_preflight_uses_podman49_template_fields_and_rejects_unsupported_hosts(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        function = shell_function("preflight")
        # Podman 4.9's Go field is CgroupsVersion; its JSON key is cgroupVersion.
        # Reject unknown templates as the real CLI does, and retain host guards.
        doubles = r'''podman() {
  if [[ $1 == --version ]]; then printf 'podman version 4.9.3\n'; return; fi
  [[ $1 == info && $2 == --format ]] || return 125
  case $3 in
    '{{.Host.Security.Rootless}}') printf '%s\n' "$fixture_rootless" ;;
    '{{.Host.CgroupsVersion}}') printf '%s\n' "$fixture_cgroups" ;;
    *) return 125 ;;
  esac
}
systemctl() { [[ $* == '--user show-environment' ]]; }
loginctl() { printf 'yes\n'; }
id() { printf 'fixture-user\n'; }
'''
        command = "set -euo pipefail\nfixture_rootless=$1\nfixture_cgroups=$2\n" + doubles + function + "\npreflight\n"
        for rootless, cgroups, succeeds in (("true", "v2", True), ("false", "v2", False), ("true", "v1", False)):
            with self.subTest(rootless=rootless, cgroups=cgroups):
                result = subprocess.run(["bash", "-c", command, "fixture", rootless, cgroups],
                                        text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)


    def test_forced_upgrade_requires_marker_schema_stopped_writers_and_old_checkpoint(self):
        doubles = r'''die() { exit 1; }
bash() {
  [[ ${SMOKE_FORCE_FAIL:-} == 1 && $* == "$REPO/scripts/upgrade.sh --no-backup" ]] || exit 92
  [[ $fault != marker ]] || { printf 'ordinary install failure\n'; return 1; }
  printf '  FAIL  SMOKE_FORCE_FAIL=1 (forced failure)\n'
  [[ $fault == succeeded ]]
}
legacy_fixture() {
  [[ $* == 'inspect za3b4c5d6e7f present' && $fault != schema ]] || return 1
  printf 'schema\n' >> "$work/proofs"
}
assert_writers_stopped() {
  [[ $fault != writers ]] || return 1
  printf 'writers\n' >> "$work/proofs"
}
checkpoint_identity() {
  [[ $1 == "$legacy_backup" && $fault != checkpoint ]] || return 1
  printf 'checkpoint\n' >> "$work/proofs"
}
'''
        command = ("set -euo pipefail\nwork=$1\nREPO=$1/candidate\nlegacy_backup=$1/old-backup\nfault=$2\n" +
                   doubles + shell_function("forced_upgrade") +
                   '\nforced_upgrade\n[[ -z ${SMOKE_FORCE_FAIL+x} ]]\n')
        for fault in ("none", "succeeded", "marker", "schema", "writers", "checkpoint"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ)
                env.pop("SMOKE_FORCE_FAIL", None)
                result = subprocess.run(["bash", "-c", command, "fixture", directory, fault],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, fault == "none", result.stderr)
                if fault == "none":
                    self.assertEqual((Path(directory) / "proofs").read_text().splitlines(),
                                     ["schema", "writers", "checkpoint"])

    def test_checkouts_bind_reviewed_head_and_exact_clean_legacy_revision(self):
        old = "431759826c6e6ea25579b05051de102699e54229"
        doubles = r'''die() { exit 1; }
git() {
  if [[ $3 == status ]]; then
    [[ $fault != dirty ]] || printf ' M Containerfile.runner\n'
    return 0
  fi
  [[ $* == "-C $2 rev-parse HEAD" ]] || return 1
  if [[ $2 == "$REPO" ]]; then
    if [[ $fault == head ]]; then printf '%040d\n' 0; else printf '%s\n' "$OMNIGENT_ROOTLESS_HEAD"; fi
  else
    if [[ $fault == old ]]; then printf '%040d\n' 0; else printf '%s\n' "$legacy_commit"; fi
  fi
}
'''
        for fault in ("none", "head", "old", "dirty", "same-path", "version", "missing-head"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / "candidate"; repo.mkdir()
                legacy = Path(directory) / "legacy"; legacy.mkdir()
                (repo / "VERSION").write_text("0.21.0-3\n")
                (legacy / "VERSION").write_text("wrong\n" if fault == "version" else "0.21.0-2\n")
                if fault == "same-path": legacy = repo
                head = "" if fault == "missing-head" else "a" * 40
                command = ("set -euo pipefail\nREPO=$1\nlegacy=$2\nlegacy_commit=$3\nOMNIGENT_ROOTLESS_HEAD=$4\nfault=$5\n" +
                           doubles + shell_function("assert_checkouts") + "\nassert_checkouts\n")
                result = subprocess.run(["bash", "-c", command, "fixture", str(repo), str(legacy), old, head, fault],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, fault == "none", result.stderr)

    def test_native_matrix_guard_rejects_emulated_or_mismatched_architecture(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        guard = 'case "$arch:' + source.split('case "$arch:', 1)[1].split('\nesac', 1)[0] + '\nesac\n'
        command = 'set -eu\narch=$1\nRUNNER_ARCH=$2\nmachine=$3\nuname() { printf "%s" "$machine"; }\ndie() { exit 1; }\n' + guard
        for arch, runner, machine, success in (("amd64", "X64", "x86_64", True),
                ("arm64", "ARM64", "aarch64", True), ("arm64", "X64", "x86_64", False),
                ("amd64", "ARM64", "aarch64", False), ("arm64", "ARM64", "x86_64", False)):
            with self.subTest(arch=arch, runner=runner, machine=machine):
                result = subprocess.run(["bash", "-c", command, "fixture", arch, runner, machine], capture_output=True)
                self.assertEqual(result.returncode == 0, success)

    def test_legacy_restore_precedes_true_upgrade_and_both_storage_restore_checks(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        sequence = 'run_stage preflight preflight' + source.split('run_stage preflight preflight', 1)[1]
        doubles = r'''die() { exit 1; }
run_stage() {
  printf '%s\n' "$*" >> "$work/stages"
  if [[ $1 == legacy-backup || $1 == backup ]]; then
    mkdir -p "$work/$3-backups/checkpoint"
    printf '%s\n' "$work/$3-backups/checkpoint" > "$work/$3-backup-path"
  fi
}
'''
        expected = ["legacy-seed", "legacy-baseline", "legacy-storage", "legacy-backup",
                    "legacy-checkpoint-identity", "legacy-backup-health", "forced-upgrade",
                    "legacy-database-mutation", "legacy-storage-mutation", "rollback-maintenance",
                    "legacy-restore", "legacy-restored", "legacy-storage-restored", "legacy-restored-identity",
                    "upgrade-A-to-B", "upgraded-legacy", "upgraded-storage", "candidate-public-base"]
        for mode in ("private", "shared"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                command = 'set -euo pipefail\nwork=$1\nREPO=$1/candidate\nlegacy=$1/legacy\npi_mode=$2\n' + doubles + sequence
                subprocess.run(["bash", "-c", command, "fixture", directory, mode], check=True, capture_output=True)
                lines = (Path(directory) / "stages").read_text().splitlines()
                stages = [line.split()[0] for line in lines]
                self.assertEqual(stages[stages.index(expected[0]):stages.index(expected[-1]) + 1], expected)
                install = next(line for line in lines if line.startswith("install-legacy "))
                self.assertIn("/legacy/scripts/install.sh --rebuild", install)
                self.assertNotIn("MCP_PUBLIC_BASE_URL", install)
                self.assertEqual("shared-peer" in stages, mode == "shared")
                self.assertLess(stages.index("candidate-writers-stopped"), stages.index("candidate-storage-mutation"))
                self.assertLess(stages.index("candidate-storage-mutation"), stages.index("restore"))
                self.assertLess(stages.index("restore"), stages.index("restored-legacy"))
                self.assertEqual(stages[-1], "source-identity")
        self.assertIn('"$REPO/scripts/backup.sh" --stop --include-secrets', shell_function("backup_checkpoint"))
        self.assertNotIn('"$legacy/scripts/backup.sh"', source)
        self.assertIn('"$REPO/scripts/backup-checkpoint.py" check', shell_function("checkpoint_identity"))
        self.assertIn('omnigent_assert_writers_stopped', shell_function("assert_writers_stopped"))
        self.assertLess(source.index('\nassert_checkouts\n'), source.index('\numask 077'))

    def test_http_fixture_refuses_external_targets(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            rootless.request("https://external.invalid/mcp/private_synthetic")

    def test_failure_code_rejects_arbitrary_text_and_enters_sanitized_report(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        function = shell_function("read_failure_code")
        reporter = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            code_path = Path(directory) / "failure-code"
            report_path = Path(directory) / "result.json"
            for value, expected in (("cookie\n", "cookie"), ("private_synthetic-secret\n", "unknown"),
                                    ("cookie\nprivate_synthetic-secret\n", "unknown")):
                with self.subTest(expected=expected):
                    code_path.write_text(value)
                    command = "set -euo pipefail\nOMNIGENT_ROOTLESS_FAILURE_FILE=$1\n" + function + "\nread_failure_code\n"
                    result = subprocess.run(["bash", "-c", command, "fixture", str(code_path)],
                                            capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout, expected)
                    subprocess.run(["python3", "-c", reporter, str(report_path), str(REPO), "1",
                                    "prepare-profiles", "true", "0", result.stdout, "arm64", "shared", "b" * 40, "a" * 40, "install", "browser-mcp"],
                                   capture_output=True, text=True, check=True)
                    report = json.loads(report_path.read_text())
                    self.assertEqual(report["failureCode"], expected)
                    self.assertEqual(report["architecture"], "linux/arm64")
                    self.assertEqual(report["piState"], "shared")
                    self.assertEqual(report["reviewedHead"], "a" * 40)
                    self.assertEqual(report["legacyPackageCommit"], "b" * 40)
                    self.assertEqual(report["completedStages"], ["install", "browser-mcp"])
                    self.assertNotIn("private_synthetic-secret", report_path.read_text())

    def test_workflow_only_uploads_explicit_sanitized_result(self):
        workflow = (REPO / ".github/workflows/rootless-acceptance.yml").read_text()
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("runs-on: ${{ matrix.runner }}", workflow)
        self.assertIn("max-parallel: 2", workflow)
        self.assertIn("fail-fast: false", workflow)
        self.assertIn("timeout-minutes: 45", workflow)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha }}", workflow)
        self.assertIn("OMNIGENT_ROOTLESS_HEAD: ${{ github.event.pull_request.head.sha }}", workflow)
        self.assertIn("ref: 431759826c6e6ea25579b05051de102699e54229", workflow)
        self.assertIn("path: .rootless-legacy", workflow)
        self.assertEqual(workflow.count("- {runner:"), 4)
        for runner, arch, machine in (("ubuntu-24.04", "amd64", "x86_64"),
                                      ("ubuntu-24.04-arm", "arm64", "aarch64")):
            for mode in ("private", "shared"):
                self.assertIn(f"runner: {runner}, arch: {arch}, machine: {machine}, pi_state: {mode}", workflow)
        self.assertIn("path: ${{ runner.temp }}/omnigent-rootless-result.json", workflow)
        self.assertNotIn("packages: write", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertEqual(workflow.count("uses: actions/upload-artifact@"), 1)


if __name__ == "__main__":
    unittest.main()
