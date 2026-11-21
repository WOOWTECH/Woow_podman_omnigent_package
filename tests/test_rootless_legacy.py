"""Offline contracts; optional installed-store checks use a disposable SQLite DB.

Run: python3 -m unittest discover -s tests -p test_rootless_legacy.py -v
Set LEGACY_FIXTURE_PYTHON to an installed baseline 0.21.0 Python to additionally
exercise real stores. LEGACY_FIXTURE_CANDIDATE_PYTHON enables an actual Alembic
A-to-B SQLite migration between worker calls. Neither proves rootless/PostgreSQL.
"""
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

SPEC = importlib.util.spec_from_file_location("legacy", Path(__file__).with_name("rootless-legacy-fixtures.py"))
legacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy)
REAL_RUN = subprocess.run


def schema(present=False):
    return {"heads": [legacy.NEW_HEAD if present else legacy.OLD_HEAD], "table": present,
            "columns": sorted(legacy.MCP_COLUMNS) if present else [],
            "indexes": [{"name": name, "unique": unique, "primary": primary,
                         "valid": True, "ready": True, "partial": False, "expression": False,
                         "columns": columns.copy()}
                        for name, (unique, primary, columns) in legacy.MCP_INDEXES.items()] if present else []}


def fixture_state():
    state = legacy.new_state()
    state["snapshot"] = {"accounts": {"owner": {"is_admin": True}, "viewer": {"is_admin": False}},
                         "sessions": {"private": {"grants": [["owner", 4]], "items": [1, 2, 3]},
                                      "shared": {"grants": [["owner", 4], ["viewer", 1]], "items": [1, 2, 3]}},
                         "task": {"state": "paused", "last_run_at": None},
                         "agent": {"version": 1}, "artifacts": {"marker": "digest"}}
    return state


class SchemaTests(unittest.TestCase):
    def test_exact_old_and_new_schemas(self):
        legacy.check_schema(schema(), legacy.OLD_HEAD, "absent")
        legacy.check_schema(schema(True), legacy.NEW_HEAD, "present")

    def test_reject_wrong_or_multiple_heads(self):
        for heads in ([], ["unexpected"], [legacy.NEW_HEAD, legacy.OLD_HEAD]):
            value = schema(True)
            value["heads"] = heads
            with self.subTest(heads=heads), self.assertRaises(legacy.FixtureError):
                legacy.check_schema(value, legacy.NEW_HEAD, "present")

    def test_reject_inconsistent_requested_pair(self):
        with self.assertRaises(legacy.FixtureError):
            legacy.check_schema(schema(), legacy.OLD_HEAD, "present")

    def test_reject_table_left_after_rollback(self):
        value = schema(True)
        value["heads"] = [legacy.OLD_HEAD]
        with self.assertRaises(legacy.FixtureError):
            legacy.check_schema(value, legacy.OLD_HEAD, "absent")

    def test_reject_missing_table_or_column(self):
        for field, value in (("table", False), ("columns", sorted(legacy.MCP_COLUMNS - {"token_hash"}))):
            body = schema(True)
            body[field] = value
            with self.subTest(field=field), self.assertRaises(legacy.FixtureError):
                legacy.check_schema(body, legacy.NEW_HEAD, "present")

    def test_reject_missing_duplicate_invalid_partial_expression_or_wrong_index(self):
        changes = [lambda rows: rows.pop(), lambda rows: rows.append(rows[0]),
                   lambda rows: rows[0].update(valid=False), lambda rows: rows[0].update(ready=False),
                   lambda rows: rows[0].update(unique=False), lambda rows: rows[0].update(primary=False),
                   lambda rows: rows[0].update(partial=True), lambda rows: rows[0].update(expression=True),
                   lambda rows: rows[0].update(columns=["id", "workspace_id"])]
        for change in changes:
            value = schema(True)
            change(value["indexes"])
            with self.subTest(value=value), self.assertRaises(legacy.FixtureError):
                legacy.check_schema(value, legacy.NEW_HEAD, "present")


class StateAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.private.chmod(0o700)
        self.path = self.private / "state.json"
        self.state = fixture_state()
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_private_new_state_roundtrip_and_no_overwrite(self):
        self.assertEqual(legacy.state_path(str(self.path)), self.path)
        legacy.save_new(self.path, self.state)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(legacy.load_state(self.path), self.state)
        with self.assertRaises(FileExistsError):
            legacy.save_new(self.path, self.state)

    def test_refuse_state_symlink_hardlink_public_mode_and_nonprivate_parent(self):
        legacy.save_new(self.path, self.state)
        link = self.private / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(legacy.FixtureError):
            legacy.state_path(str(link))
        with self.assertRaises(OSError):
            legacy.load_state(link)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(legacy.FixtureError):
            legacy.load_state(self.path)
        link.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(legacy.FixtureError):
            legacy.load_state(self.path)
        self.private.chmod(0o755)
        with self.assertRaises(legacy.FixtureError):
            legacy.state_path(str(self.path))

    def test_refuse_state_outside_temp_or_directly_in_temp(self):
        for value in (str(self.root / "state.json"), "/tmp/legacy-invalid.json"):
            with self.subTest(path=value), self.assertRaises(legacy.FixtureError):
                legacy.state_path(value)

    def test_ids_are_validated_before_sql(self):
        for key in ("agent", "task"):
            state = copy.deepcopy(self.state)
            state[key] = "x'; DROP TABLE users; --"
            with self.assertRaises(legacy.FixtureError):
                legacy.mutation_sql(state)
        self.state["viewer"]["id"] += "'"
        with self.assertRaises(legacy.FixtureError):
            legacy.mutation_sql(self.state)

    def test_mutation_is_scoped_transactional_and_bounded(self):
        sql = legacy.mutation_sql(self.state)
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertEqual(sql.count("GET DIAGNOSTICS n=ROW_COUNT"), 3)
        self.assertEqual(sql.count("IF n<>1"), 3)
        self.assertIn("SET LOCAL statement_timeout='15s'", sql)
        self.assertNotIn("DROP ", sql)
        self.assertNotIn("ALTER ", sql)
        self.assertIn("workspace_id=0", sql)
        self.assertEqual(sql.count("decode('" + self.state["sessions"]["shared"] + "','hex')"), 4)
        self.assertEqual(sql.count("decode('" + self.state["task"] + "','hex')"), 2)
        self.assertNotIn("prompt=", sql)
        self.assertIn("SET name='legacy-mutated-after-checkpoint'", sql)

    def test_non_disposable_host_refused_before_tools(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(legacy, "run") as run:
            with self.assertRaises(legacy.FixtureError):
                legacy.guard()
            run.assert_not_called()

    def test_schema_failure_prevents_worker_and_mutation(self):
        legacy.save_new(self.path, self.state)
        for operation in ("verify", "inspect", "mutate"):
            with patch.object(legacy, "guard"), patch.object(legacy, "psql", return_value=schema()), \
                 patch.object(legacy, "worker") as worker, patch.object(legacy, "require_stopped_writers") as stopped:
                with self.subTest(operation=operation), self.assertRaises(legacy.FixtureError):
                    legacy.execute(operation, str(self.path), legacy.NEW_HEAD, "present")
                worker.assert_not_called()
                stopped.assert_not_called()

    def test_mutate_requires_stopped_writers_before_database_write(self):
        legacy.save_new(self.path, self.state)
        with patch.object(legacy, "guard"), patch.object(legacy, "inspect_schema"), \
             patch.object(legacy, "require_stopped_writers", side_effect=legacy.FixtureError), \
             patch.object(legacy, "psql") as psql:
            with self.assertRaises(legacy.FixtureError):
                legacy.execute("mutate", str(self.path), legacy.NEW_HEAD, "present")
            psql.assert_not_called()

    def test_inspect_does_not_execute_application_or_mutate(self):
        legacy.save_new(self.path, self.state)
        with patch.object(legacy, "guard"), patch.object(legacy, "psql", return_value=schema(True)) as psql, \
             patch.object(legacy, "worker") as worker:
            legacy.execute("inspect", str(self.path), legacy.NEW_HEAD, "present")
            psql.assert_called_once_with(legacy.SCHEMA_SQL)
            worker.assert_not_called()

    def test_fixed_output_hides_exceptions_and_invalid_arguments(self):
        from urllib.parse import urlunsplit
        userinfo = ":".join(("fixture-user", "synthetic-private-value"))
        secret = urlunsplit(("postgresql", "@".join((userinfo, "private.invalid")), "/db", "", ""))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(legacy, "execute", side_effect=RuntimeError(secret)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = legacy.main(["seed", str(self.path), "--expected-head", legacy.OLD_HEAD, "--mcp", "absent"])
        self.assertEqual(result, 1)
        self.assertEqual(out.getvalue(), "FAIL legacy-arguments\n")
        self.assertEqual(err.getvalue(), "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(legacy.main([secret]), 1)
        self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_subprocess_has_private_pipes_timeout_and_no_shell(self):
        with patch.object(legacy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "private")) as run:
            legacy.run(["podman", "exec"], data="synthetic-private-input")
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertNotIn("shell", kwargs)

    def test_snapshot_negative_controls(self):
        baseline = self.state["snapshot"]
        changes = [lambda s: s["accounts"]["viewer"].update(is_admin=True),
                   lambda s: s["sessions"]["private"]["grants"].append(["viewer", 1]),
                   lambda s: s["sessions"]["shared"].update(grants=[]),
                   lambda s: s["sessions"]["shared"]["items"].reverse(),
                   lambda s: s["sessions"]["shared"]["items"].pop(),
                   lambda s: s["task"].update(state="active"),
                   lambda s: s["task"].update(last_run_at=1),
                   lambda s: s["agent"].update(version=2),
                   lambda s: s["artifacts"].update(marker="changed")]
        legacy.compare_snapshot(baseline, copy.deepcopy(baseline))
        for change in changes:
            actual = copy.deepcopy(baseline)
            change(actual)
            with self.subTest(actual=actual), self.assertRaises(legacy.FixtureError):
                legacy.compare_snapshot(baseline, actual)


class WritersAndHTTPTests(unittest.TestCase):
    def writer_run(self, args, **kwargs):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=\n", "")
        if "exists" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, '{"Running":false,"Pid":0}', "")

    def test_stopped_writers(self):
        with patch.object(legacy, "run", side_effect=self.writer_run):
            legacy.require_stopped_writers()

    def test_active_process_pending_job_unknown_container_status_fail_closed(self):
        changes = [("ActiveState=inactive", "ActiveState=active"), ("MainPID=0", "MainPID=123"),
                   ("Job=\n", "Job=12\n"), ("LoadState=loaded", "LoadState=not-found")]
        for before, after in changes:
            def run(args, **kwargs):
                value = self.writer_run(args, **kwargs)
                value.stdout = value.stdout.replace(before, after)
                return value
            with self.subTest(change=after), patch.object(legacy, "run", side_effect=run):
                with self.assertRaises(legacy.FixtureError):
                    legacy.require_stopped_writers()
        def unknown(args, **kwargs):
            if "exists" in args:
                return subprocess.CompletedProcess(args, 125, "", "")
            return self.writer_run(args, **kwargs)
        with patch.object(legacy, "run", side_effect=unknown), self.assertRaises(legacy.FixtureError):
            legacy.require_stopped_writers()

    def test_running_container_fails_even_when_service_inactive(self):
        def run(args, **kwargs):
            value = self.writer_run(args, **kwargs)
            if "inspect" in args:
                value.stdout = '{"Running":true,"Pid":321}'
            return value
        with patch.object(legacy, "run", side_effect=run), self.assertRaises(legacy.FixtureError):
            legacy.require_stopped_writers()

    def test_redirect_is_rejected(self):
        with self.assertRaises(legacy.FixtureError):
            legacy.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid/")

    def test_http_private_denial_and_shared_read_required(self):
        state = fixture_state()
        def request(path, *, body=None, token=None):
            if path == "/auth/login":
                return 200, {"token": body["username"]}
            if path == "/auth/me":
                return 200, {"id": token, "is_admin": token == state["owner"]["id"]}
            if token == state["owner"]["id"] or path.endswith(state["sessions"]["shared"]):
                return 200, {}
            return 404, None
        with patch.object(legacy, "request", side_effect=request):
            legacy.verify_http(state)
        def leaky(path, **kwargs):
            if path.endswith(state["sessions"]["private"]):
                return 200, {}
            return request(path, **kwargs)
        with patch.object(legacy, "request", side_effect=leaky), self.assertRaises(legacy.FixtureError):
            legacy.verify_http(state)


@unittest.skipUnless(os.environ.get("LEGACY_FIXTURE_PYTHON"), "optional installed baseline interpreter not configured")
class InstalledStoreTests(unittest.TestCase):
    def test_real_seed_verify_migration_and_negative_controls(self):
        baseline = os.environ["LEGACY_FIXTURE_PYTHON"]
        candidate = os.environ.get("LEGACY_FIXTURE_CANDIDATE_PYTHON")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "DATABASE_URL": "sqlite:///" + str(root / "fixture.db"),
                   "ARTIFACT_DIR": str(root / "artifacts")}
            def python(code, *, interpreter=baseline, data=None, success=True):
                result = REAL_RUN([interpreter, "-I", "-c", code], input=data, capture_output=True,
                                  text=True, env=env, cwd=root, timeout=120)
                if success:
                    self.assertEqual(result.returncode, 0, "installed-store subprocess failed: " + result.stderr[-2000:])
                else:
                    self.assertNotEqual(result.returncode, 0)
                return result
            # Test setup explicitly initializes this disposable database. The
            # production fixture worker itself must not run migrations.
            initialize = "import os; from omnigent.db.utils import get_or_create_engine; get_or_create_engine(os.environ['DATABASE_URL'])"
            python(initialize)
            state = legacy.new_state()
            def call(operation, interpreter=baseline, success=True):
                return python(legacy.WORKER, interpreter=interpreter,
                              data=json.dumps({"operation": operation, "state": state}), success=success)
            state["snapshot"] = json.loads(call("seed").stdout)
            actual = json.loads(call("verify").stdout)
            legacy.compare_snapshot(state["snapshot"], actual)
            self.assertEqual(len(actual["sessions"]["shared"]["items"]), 6)
            self.assertEqual(actual["task"]["state"], "paused")
            self.assertIsNone(actual["task"]["next_run_at"])
            if candidate:
                # Candidate worker must refuse the old DB, not migrate it.
                call("verify", interpreter=candidate, success=False)
                python(initialize, interpreter=candidate)
                actual = json.loads(call("verify", interpreter=candidate).stdout)
                legacy.compare_snapshot(state["snapshot"], actual)
                baseline = candidate
            marker = root / "artifacts" / "legacy-ci" / state["nonce"] / "marker.txt"
            old = marker.read_bytes()
            marker.write_bytes(b"changed")
            changed = json.loads(call("verify", interpreter=baseline).stdout)
            with self.assertRaises(legacy.FixtureError):
                legacy.compare_snapshot(state["snapshot"], changed)
            marker.write_bytes(old)
            # Real ACL corruption is rejected inside the installed-store worker.
            python("import os; from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore; "
                   "SqlAlchemyPermissionStore(os.environ['DATABASE_URL']).grant(" +
                   repr(state["viewer"]["id"]) + "," + repr(state["sessions"]["private"]) + ",1)",
                   interpreter=baseline)
            call("verify", interpreter=baseline, success=False)


PG_DISPOSABLE_PURPOSE = "disposable-legacy-pg-fixture"


def postgres_target(config):
    """Require an explicit target; never fall back to the current namespace."""
    import re
    legacy.require(isinstance(config, dict))
    legacy.require(config.get("purpose") == PG_DISPOSABLE_PURPOSE)
    legacy.require(isinstance(config.get("context"), str) and
                   re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,252}", config["context"]) is not None)
    for field in ("namespace", "pod"):
        value = config.get(field)
        legacy.require(isinstance(value, str) and len(value) <= 63 and
                       re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value) is not None)
    legacy.require(isinstance(config.get("podUid"), str) and
                   re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config["podUid"]) is not None)
    return ["kubectl", "--context", config["context"], "-n", config["namespace"]], config["pod"]


def check_disposable_postgres_pod(value, config, image):
    """Verify ownership and ephemeral storage before any database operation."""
    postgres_target(config)
    metadata, spec = value["metadata"], value["spec"]
    legacy.require(metadata["uid"] == config["podUid"])
    legacy.require(metadata["name"] == config["pod"] and metadata["namespace"] == config["namespace"])
    legacy.require(metadata.get("labels", {}).get("purpose") == PG_DISPOSABLE_PURPOSE)
    legacy.require(spec.get("automountServiceAccountToken") is False)
    volumes = spec.get("volumes", [])
    legacy.require(bool(volumes) and all(set(v) == {"name", "emptyDir"} for v in volumes))
    legacy.require(len(spec["containers"]) == 1 and spec["containers"][0]["image"] == image)


class PostgreSQLOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.config = {"context": "ci-context", "namespace": "dev-sandbox-fixtures",
                       "pod": "legacy-pg-fixture", "podUid": "01234567-89ab-cdef-0123-456789abcdef",
                       "purpose": PG_DISPOSABLE_PURPOSE}
        self.pod = {"metadata": {"name": self.config["pod"], "namespace": self.config["namespace"],
                    "uid": self.config["podUid"], "labels": {"purpose": PG_DISPOSABLE_PURPOSE}},
                    "spec": {"automountServiceAccountToken": False,
                             "volumes": [{"name": "data", "emptyDir": {}}],
                             "containers": [{"image": "synthetic-pinned-postgres"}]}}

    def test_explicit_generic_target_and_owned_ephemeral_pod(self):
        self.assertEqual(postgres_target(self.config),
                         (["kubectl", "--context", "ci-context", "-n", "dev-sandbox-fixtures"], "legacy-pg-fixture"))
        check_disposable_postgres_pod(self.pod, self.config, "synthetic-pinned-postgres")

    def test_missing_or_invalid_target_is_rejected(self):
        for field in ("context", "namespace", "pod", "podUid", "purpose"):
            config = dict(self.config)
            del config[field]
            with self.subTest(missing=field), self.assertRaises(legacy.FixtureError):
                postgres_target(config)
        for field, value in (("context", "--context=other"), ("namespace", "../other"),
                             ("pod", "pod/other"), ("podUid", ""), ("purpose", "application")):
            with self.subTest(field=field), self.assertRaises(legacy.FixtureError):
                postgres_target({**self.config, field: value})

    def test_changed_identity_purpose_storage_or_service_account_is_rejected(self):
        changes = [lambda p: p["metadata"].update(uid="another-uid"),
                   lambda p: p["metadata"].update(name="another-pod"),
                   lambda p: p["metadata"].update(namespace="another-namespace"),
                   lambda p: p["metadata"]["labels"].update(purpose="application"),
                   lambda p: p["spec"].update(automountServiceAccountToken=True),
                   lambda p: p["spec"].pop("automountServiceAccountToken"),
                   lambda p: p["spec"].update(volumes=[]),
                   lambda p: p["spec"].update(volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "existing"}}]),
                   lambda p: p["spec"]["volumes"][0].update(hostPath={"path": "/data"}),
                   lambda p: p["spec"]["containers"][0].update(image="other-image")]
        for change in changes:
            pod = copy.deepcopy(self.pod)
            change(pod)
            with self.subTest(pod=pod), self.assertRaises(legacy.FixtureError):
                check_disposable_postgres_pod(pod, self.config, "synthetic-pinned-postgres")


@unittest.skipUnless(os.environ.get("LEGACY_FIXTURE_PG_CONFIG"), "optional disposable PostgreSQL Pod not configured")
class PostgreSQLStoreTests(unittest.TestCase):
    """Real PostgreSQL regression on the explicitly authorized disposable Pod.

    Caller creates/deletes a disposable Pod and owns its loopback port-forward.
    The owned 0600 JSON config supplies context, namespace, pod, podUid,
    purpose="disposable-legacy-pg-fixture" (also required as the Pod purpose label),
    databaseUrl (postgresql+psycopg2://...@127.0.0.1:PORT/postgres), baselinePython,
    candidatePython and optional evidencePath. The baseline interpreter must have
    psycopg2 installed in a task-only venv. This test owns one random database;
    it never reuses an existing DB. It does not bypass the helper's main guards.
    """

    def test_bytea_mutation_rollback_and_full_old_checkpoint_restore(self):
        import re
        from urllib.parse import urlsplit
        config_path = Path(os.environ["LEGACY_FIXTURE_PG_CONFIG"])
        self.assertFalse(config_path.is_symlink())
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        config = json.loads(config_path.read_text())
        self.assertEqual(config_path.stat().st_uid, os.getuid())
        kube, pod = postgres_target(config)
        parsed = urlsplit(config["databaseUrl"])
        self.assertEqual(parsed.scheme, "postgresql+psycopg2")
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertTrue(parsed.port and parsed.password)
        self.assertEqual(parsed.username, "omnigent")
        self.assertEqual(parsed.path, "/postgres")
        phases = []

        def command(args, *, data=None, success=True):
            result = REAL_RUN(args, input=data, capture_output=True, timeout=120)
            self.assertEqual(result.returncode == 0, success, "private PostgreSQL subprocess failed")
            return result

        def owned_pod():
            value = json.loads(command(kube + ["get", "pod", pod, "-o", "json"]).stdout)
            expected = next(line.split("=", 1)[1] for line in
                (Path(__file__).parents[1] / "quadlet/omnigent-postgres.container").read_text().splitlines()
                if line.startswith("Image="))
            check_disposable_postgres_pod(value, config, expected)
            return value

        owned_pod()
        database = "legacy_fixture_" + os.urandom(8).hex()
        self.assertIsNotNone(re.fullmatch(r"legacy_fixture_[0-9a-f]{16}", database))
        prefix = kube + ["exec", "-i", pod, "--"]

        def sql(statement, *, db=database, success=True):
            owned_pod()
            result = command(prefix + ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                                       "-U", "omnigent", "-d", db],
                             data=statement.encode(), success=success)
            return result.stdout.decode().strip()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "DATABASE_URL": config["databaseUrl"].rsplit("/", 1)[0] + "/" + database,
                   "ARTIFACT_DIR": str(root / "artifacts")}
            baseline, candidate = config["baselinePython"], config["candidatePython"]
            initialize = "import os; from omnigent.db.utils import get_or_create_engine; get_or_create_engine(os.environ['DATABASE_URL'])"

            def python(code, interpreter, data=None, success=True):
                result = REAL_RUN([interpreter, "-I", "-c", code], input=data, capture_output=True,
                                  text=True, env=env, cwd=root, timeout=120)
                self.assertEqual(result.returncode == 0, success, "private installed-store subprocess failed")
                return result

            state = legacy.new_state()
            def worker(operation, interpreter, success=True):
                return python(legacy.WORKER, interpreter,
                              json.dumps({"operation": operation, "state": state}), success)

            created = False
            try:
                sql("CREATE DATABASE " + database + ";", db="postgres")
                created = True
                python(initialize, baseline)
                legacy.check_schema(json.loads(sql(legacy.SCHEMA_SQL)), legacy.OLD_HEAD, "absent")
                state["snapshot"] = json.loads(worker("seed", baseline).stdout)
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", baseline).stdout))
                phases.append("real-baseline-store-seed-and-verify")
                owned_pod()
                checkpoint = command(prefix + ["pg_dump", "-Fc", "-U", "omnigent", "-d", database]).stdout
                self.assertTrue(checkpoint.startswith(b"PGDMP"))
                phases.append("old-custom-format-checkpoint")
                python(initialize, candidate)
                legacy.check_schema(json.loads(sql(legacy.SCHEMA_SQL)), legacy.NEW_HEAD, "present")
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", candidate).stdout))
                phases.append("actual-A-to-B-migration-schema-indexes-and-retention")
                task = state["task"]
                predicate = " WHERE workspace_id=0 AND id=decode('" + task + "','hex')"
                types = json.loads(sql("SELECT json_build_object('idType',pg_typeof(id)::text,"
                    "'idBytes',octet_length(id),'promptType',pg_typeof(prompt)::text) "
                    "FROM public.scheduled_tasks" + predicate + ";"))
                self.assertEqual(types, {"idType": "bytea", "idBytes": 16, "promptType": "bytea"})
                prompt_sql = "SELECT encode(prompt,'hex') FROM public.scheduled_tasks" + predicate + ";"
                before_prompt = sql(prompt_sql)
                mutation = legacy.mutation_sql(state)
                # Reproduce the reviewed bug against actual binary IDs. Its
                # failed transaction must leave all baseline data unchanged.
                broken = re.sub(r"decode\('([0-9a-f]{32})','hex'\)", r"'\1'", mutation)
                sql(broken, success=False)
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", candidate).stdout))
                phases.append("hex-literal-bug-reproduced-without-data-change")
                rolled_back = json.loads(sql(mutation.replace("COMMIT;", "ROLLBACK;")))
                self.assertEqual(rolled_back, {"changed": False})
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", candidate).stdout))
                phases.append("real-transaction-rollback-preserves-snapshot")
                self.assertEqual(json.loads(sql(mutation)), {"changed": True})
                worker("verify", candidate, success=False)
                self.assertEqual(before_prompt, sql(prompt_sql))
                task_check = "import os; from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore; " \
                    "t=SqlAlchemyScheduledTaskStore(os.environ['DATABASE_URL']).get(" + repr(task) + "); " \
                    "assert t.name=='legacy-mutated-after-checkpoint' and t.prompt=='Never run this fixture.' and t.state=='paused'; print('ok')"
                self.assertEqual(python(task_check, candidate).stdout.strip(), "ok")
                phases.append("fixed-mutation-detected-and-compressed-prompt-preserved")
                # Actual full old checkpoint restoration, not a schema downgrade.
                owned_pod()
                command(prefix + ["dropdb", "-U", "omnigent", database])
                command(prefix + ["createdb", "-U", "omnigent", database])
                command(prefix + ["pg_restore", "--exit-on-error", "-U", "omnigent", "-d", database], data=checkpoint)
                legacy.check_schema(json.loads(sql(legacy.SCHEMA_SQL)), legacy.OLD_HEAD, "absent")
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", baseline).stdout))
                phases.append("full-pg-restore-old-schema-and-original-snapshot")
                python(initialize, candidate)
                legacy.check_schema(json.loads(sql(legacy.SCHEMA_SQL)), legacy.NEW_HEAD, "present")
                legacy.compare_snapshot(state["snapshot"], json.loads(worker("verify", candidate).stdout))
                phases.append("restored-A-upgrades-successfully-again")
            finally:
                if created:
                    owned_pod()
                    command(prefix + ["dropdb", "--if-exists", "-U", "omnigent", database])
            phases.append("test-database-cleaned")
        if config.get("evidencePath"):
            fd = os.open(config["evidencePath"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"status": "passed", "checks": phases, "oldHead": legacy.OLD_HEAD,
                           "newHead": legacy.NEW_HEAD, "postgresRuntimeVerified": True,
                           "rootlessVerified": False}, out, indent=2)


if __name__ == "__main__":
    unittest.main()
