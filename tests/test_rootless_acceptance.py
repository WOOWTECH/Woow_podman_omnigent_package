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


class RootlessFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private-state.json"
        self.rows = {}
        self.markers = {}
        self.count = 0
        self.cookie = "a" * 64
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
        self.markers[args[-1]] = args[-2].encode()
        return subprocess.CompletedProcess(args, 0)

    def output(self, args, **kwargs):
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
        function = "run_stage() {" + source.split("run_stage() {", 1)[1].split("\npreflight()", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "unexpected"
            command = ("set -e\nwork=$1\ncompleted=()\nsystemctl() { :; }\n" + function +
                       "\nfail_early() { false; touch \"$work/unexpected\"; }\nrun_stage fixture fail_early\n")
            result = subprocess.run(["bash", "-c", command, "fixture", directory], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_preflight_uses_podman49_template_fields_and_rejects_unsupported_hosts(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        function = "preflight() {" + source.split("preflight() {", 1)[1].split("\nrun_stage preflight", 1)[0]
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

    def test_http_fixture_refuses_external_targets(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            rootless.request("https://external.invalid/mcp/private_synthetic")

    def test_failure_code_rejects_arbitrary_text_and_enters_sanitized_report(self):
        source = (REPO / "tests/rootless-acceptance.sh").read_text()
        function = "read_failure_code() {" + source.split("read_failure_code() {", 1)[1].split("\nfinish()", 1)[0]
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
                                    "prepare-profiles", "true", "0", result.stdout, "install", "browser-mcp"],
                                   capture_output=True, text=True, check=True)
                    report = json.loads(report_path.read_text())
                    self.assertEqual(report["failureCode"], expected)
                    self.assertEqual(report["completedStages"], ["install", "browser-mcp"])
                    self.assertNotIn("private_synthetic-secret", report_path.read_text())

    def test_workflow_only_uploads_explicit_sanitized_result(self):
        workflow = (REPO / ".github/workflows/rootless-acceptance.yml").read_text()
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("path: ${{ runner.temp }}/omnigent-rootless-result.json", workflow)
        self.assertNotIn("packages: write", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertEqual(workflow.count("uses: actions/upload-artifact@"), 1)


if __name__ == "__main__":
    unittest.main()
