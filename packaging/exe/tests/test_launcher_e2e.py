"""End-to-end test: run the launcher as a real subprocess against a fake app.

This is the load-bearing test for the whole pipeline. It proves that a frozen
``paperclip`` binary will:

* discover a Node runtime and an app payload it was not told about explicitly,
* forward arguments verbatim to the real bundle,
* let the app see its own environment and working directory,
* propagate the app's exit code out of the launcher.

The "app" is a POSIX shell stand-in for ``node`` plus a JS file that the stand-in
interprets, so the test needs no Node.js installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

LAUNCHER = [sys.executable, "-m", "paperclip_exe.launcher"]


#: Version the fake Node reports, chosen to satisfy MINIMUM_NODE_VERSION.
FAKE_NODE_VERSION = "v24.11.0"

#: Stand-in for the real ``node`` binary. Placeholders are substituted at write
#: time so the shell script stays readable (no nested f-string indentation).
_FAKE_NODE_TEMPLATE = r"""#!/bin/sh
# fake node used by the launcher end-to-end tests

# 1. The launcher probes candidates with `--version` before trusting them.
case "$1" in
  --version) echo "@VERSION@"; exit 0 ;;
esac

# 2. Node options precede the entry point, so skip leading --flags to find it.
node_options=""
entry=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --*) node_options="$node_options|$1"; shift ;;
    *) entry="$1"; shift; break ;;
  esac
done

# 3. Record how we were invoked, and honour --exit-code=N from the app args.
code=0
args=""
for arg in "$@"; do
  args="$args|$arg"
  case "$arg" in
    --exit-code=*) code="${arg#--exit-code=}" ;;
  esac
done

"@PYTHON@" - "$entry" "$args" "$node_options" > "@REPORT@" <<'PY'
import json, os, sys
entry, args, node_options = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "entry": entry,
    "args": [a for a in args.split("|") if a],
    "node_options": [o for o in node_options.split("|") if o],
    "cwd": os.getcwd(),
    "launcher": os.environ.get("PAPERCLIP_EXE_LAUNCHER"),
    "node_path_first": (os.environ.get("PATH") or "").split(os.pathsep)[0],
    "node_path_env": os.environ.get("NODE_PATH"),
}, indent=2))
PY

exit "$code"
"""


def write_fake_node(directory: Path, *, report_file: Path, version: str = FAKE_NODE_VERSION) -> Path:
    """A stand-in ``node`` that records how it was invoked and honours exit codes.

    Contract:
      ``fake-node --version``           -> prints ``version``, exit 0
      ``fake-node <entry.js> [args...]``-> writes a JSON report of argv, cwd and
                                          interesting env vars, then exits with
                                          the code given by ``--exit-code=N``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "node"
    script = _FAKE_NODE_TEMPLATE.replace("@VERSION@", version)
    script = script.replace("@REPORT@", str(report_file))
    script = script.replace("@PYTHON@", sys.executable)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def write_fake_payload(root: Path, *, with_node_modules: bool = True) -> Path:
    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    entry = app / "index.js"
    entry.write_text("// pretend esbuild bundle\n", encoding="utf-8")
    (app / "package.json").write_text(
        json.dumps({"name": "paperclipai", "version": "0.3.1-test"}), encoding="utf-8"
    )
    if with_node_modules:
        modules = root / "node_modules" / "commander"
        modules.mkdir(parents=True, exist_ok=True)
        (modules / "package.json").write_text('{"name":"commander"}', encoding="utf-8")
    return entry


def run_launcher(args, env_overrides=None, cwd=None, isolated=False):
    """Invoke the launcher in a subprocess.

    ``isolated=True`` runs with ``python -P`` from an empty directory so the
    launcher cannot discover a payload via its source-tree/CWD fallbacks. Use it
    to test the genuinely-no-payload error path.
    """
    env = dict(os.environ)
    # Isolate from the developer's real machine.
    for key in ("PAPERCLIP_NODE", "PAPERCLIP_PAYLOAD", "PAPERCLIP_ENTRY", "PAPERCLIP_DATA_DIR",
                "PAPERCLIP_NODE_OPTIONS", "PAPERCLIP_FETCH_NODE", "PAPERCLIP_LAUNCHER_DEBUG",
                "PAPERCLIP_STRICT_PAYLOAD"):
        env.pop(key, None)
    # `-m` resolves the package from sys.path; when cwd differs from the package
    # parent we must supply PYTHONPATH explicitly.
    env["PYTHONPATH"] = str(HERE)
    if env_overrides:
        env.update({k: v for k, v in env_overrides.items() if v is not None})
    if isolated:
        # -P keeps the cwd off sys.path so no stray local package shadows ours.
        command = [sys.executable, "-P", "-m", "paperclip_exe.launcher"]
        run_cwd = cwd or Path(env.get("PAPERCLIP_EMPTY_CWD", HERE))
    else:
        command = LAUNCHER
        run_cwd = cwd or HERE
    return subprocess.run(
        command + list(args),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(run_cwd),
        check=False,
    )


@unittest.skipIf(os.name == "nt", "fake node is a POSIX shell script")
class LauncherEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.report = self.tmp / "report.json"
        self.node_dir = self.tmp / "nodebin"
        self.node = write_fake_node(self.node_dir, report_file=self.report)
        self.payload_root = self.tmp / "payload"
        self.entry = write_fake_payload(self.payload_root)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self):
        return {
            "PAPERCLIP_NODE": str(self.node),
            "PAPERCLIP_PAYLOAD": str(self.payload_root),
            "PAPERCLIP_DATA_DIR": str(self.tmp / "data"),
        }

    def test_forwards_arguments_verbatim(self):
        completed = run_launcher(
            ["issue", "list", "--company", "acme"],
            self._env(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(Path(report["entry"]), self.entry)
        self.assertEqual(report["args"], ["issue", "list", "--company", "acme"])

    def test_runs_with_entry_as_working_directory(self):
        completed = run_launcher(["doctor"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(Path(report["cwd"]), self.entry.parent)

    def test_child_is_marked_and_gets_node_path(self):
        completed = run_launcher(["doctor"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["launcher"], "1")
        self.assertEqual(Path(report["node_path_first"]), self.node_dir)
        self.assertIn(str(self.payload_root / "node_modules"), report["node_path_env"] or "")

    def test_propagates_nonzero_exit_code(self):
        completed = run_launcher(["run", "--exit-code=3"], self._env())
        self.assertEqual(completed.returncode, 3)

    def test_propagates_zero_exit_code(self):
        completed = run_launcher(["run", "--exit-code=0"], self._env())
        self.assertEqual(completed.returncode, 0)

    def test_node_options_env_is_injected_before_entry(self):
        env = self._env()
        env["PAPERCLIP_NODE_OPTIONS"] = "--max-old-space-size=4096 --enable-source-maps"
        completed = run_launcher(["doctor"], env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(
            report["node_options"],
            ["--max-old-space-size=4096", "--enable-source-maps"],
        )
        # The entry point must still be the bundle, not the Node option.
        self.assertEqual(Path(report["entry"]), self.entry)
        self.assertEqual(report["args"], ["doctor"])

    def test_launcher_flag_is_not_forwarded(self):
        completed = run_launcher(
            ["--launcher-fetch-node", "company", "list", "--exit-code=0"],
            self._env(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertNotIn("--launcher-fetch-node", report["args"])
        self.assertEqual(report["args"], ["company", "list", "--exit-code=0"])

    def test_double_dash_sends_launcher_flag_to_app(self):
        completed = run_launcher(["--", "--launcher-info"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["args"], ["--launcher-info"])

    def test_info_returns_valid_json_without_running_app(self):
        completed = run_launcher(["--launcher-info"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        info = json.loads(completed.stdout)
        self.assertEqual(info["launcher_version"], "1.0.0")
        self.assertEqual(info["minimum_node_version"], "24.11.0")
        self.assertIsNotNone(info["node"])
        self.assertEqual(info["payload"]["app_version"], "0.3.1-test")
        self.assertFalse(self.report.exists(), "app must not run for --launcher-info")

    def test_doctor_reports_healthy(self):
        completed = run_launcher(["--launcher-doctor"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("ready to run", completed.stdout)
        self.assertIn("Node runtime", completed.stdout)
        self.assertIn("Application payload", completed.stdout)
        self.assertFalse(self.report.exists(), "app must not run for --launcher-doctor")

    def test_doctor_flags_missing_node_modules(self):
        # Rewrite payload without node_modules.
        import shutil

        shutil.rmtree(self.payload_root / "node_modules", ignore_errors=True)
        completed = run_launcher(["--launcher-doctor"], self._env())
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MISSING", completed.stdout)
        self.assertIn("not ready", completed.stdout)

    def test_missing_node_gives_actionable_error(self):
        env = self._env()
        env["PAPERCLIP_NODE"] = str(self.tmp / "does-not-exist" / "node")
        env["PATH"] = str(self.tmp / "empty-bin")
        completed = run_launcher(["doctor"], env)
        self.assertEqual(completed.returncode, 78)
        self.assertIn("24.11.0", completed.stderr)
        self.assertIn("--launcher-doctor", completed.stderr)

    def test_missing_payload_gives_actionable_error(self):
        # Run isolated (python -P, empty cwd) so the source-tree and cwd payload
        # fallbacks cannot mask the failure.
        empty_cwd = self.tmp / "empty-cwd"
        empty_cwd.mkdir()
        env = self._env()
        env["PAPERCLIP_PAYLOAD"] = str(self.tmp / "no-such-payload")
        env["PAPERCLIP_STRICT_PAYLOAD"] = "1"
        env["PAPERCLIP_EMPTY_CWD"] = str(empty_cwd)
        completed = run_launcher(["doctor"], env, cwd=empty_cwd, isolated=True)
        self.assertEqual(completed.returncode, 78, completed.stdout + completed.stderr)
        self.assertIn("payload", completed.stderr.lower())
        self.assertIn("app/index.js", completed.stderr)
        self.assertIn("PAPERCLIP_PAYLOAD", completed.stderr)
        self.assertFalse(self.report.exists(), "app must not run without a payload")

    def test_falls_back_to_cwd_payload_when_env_unset(self):
        # Documents the developer ergonomics: pointing the launcher at a payload
        # via the working directory works without any env var.
        env = {
            "PAPERCLIP_NODE": str(self.node),
            "PAPERCLIP_DATA_DIR": str(self.tmp / "data"),
        }
        completed = run_launcher(["doctor"], env, cwd=self.payload_root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(Path(report["entry"]), self.entry)

    def test_version_flag_reports_all_three_versions(self):
        completed = run_launcher(["--launcher-version"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("paperclip-launcher 1.0.0", completed.stdout)
        self.assertIn("paperclip app 0.3.1-test", completed.stdout)

    def test_help_flag_prints_usage_without_running_app(self):
        completed = run_launcher(["--launcher-help"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--launcher-doctor", completed.stdout)
        self.assertIn("PAPERCLIP_NODE", completed.stdout)
        self.assertFalse(self.report.exists())

    def test_node_below_minimum_is_rejected(self):
        # The version gate must actually fire end-to-end, not just in unit tests:
        # this sandbox has Node 22 while the app requires >= 24.11.0.
        old_node = write_fake_node(self.tmp / "oldnode", report_file=self.report, version="v22.22.3")
        env = self._env()
        env["PAPERCLIP_NODE"] = str(old_node)
        env["PATH"] = str(self.tmp / "empty-bin")
        completed = run_launcher(["doctor"], env)
        self.assertEqual(completed.returncode, 78)
        self.assertIn("24.11.0", completed.stderr)
        self.assertIn("22.22.3", completed.stderr)
        self.assertFalse(self.report.exists(), "app must not run on an unsupported Node")

    def test_separate_launcher_node_value_is_consumed(self):
        # Regression: `--launcher-node <path>` must not treat <path> as the app's
        # first argument.
        alternate = write_fake_node(self.tmp / "alt-node", report_file=self.report)
        completed = run_launcher(
            ["--launcher-node", str(alternate), "doctor"],
            {"PAPERCLIP_PAYLOAD": str(self.payload_root), "PAPERCLIP_DATA_DIR": str(self.tmp / "data")},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["args"], ["doctor"])
        self.assertNotIn(str(alternate), report["args"])


@unittest.skipIf(os.name == "nt", "fake node is a POSIX shell script")
class LauncherPostgresReportingTests(unittest.TestCase):
    """The launcher must surface embedded-Postgres health, not hide it.

    The app resolves ``@embedded-postgres/<slug>`` as a sibling of
    ``embedded-postgres`` (packages/db/src/embedded-postgres-native.ts), so a
    staged tree that loses that relationship silently breaks local-DB flows.
    ``--launcher-info`` and ``--launcher-doctor`` are where that becomes visible.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.report = self.tmp / "report.json"
        self.node = write_fake_node(self.tmp / "nodebin", report_file=self.report)
        self.payload_root = self.tmp / "payload"
        self.entry = write_fake_payload(self.payload_root)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self):
        return {
            "PAPERCLIP_NODE": str(self.node),
            "PAPERCLIP_PAYLOAD": str(self.payload_root),
            "PAPERCLIP_DATA_DIR": str(self.tmp / "data"),
        }

    def _add_native_postgres(self, slug: str, *, wrapper: bool = True, binaries=("initdb", "postgres", "pg_ctl")):
        modules = self.payload_root / "node_modules"
        pkg = modules / "@embedded-postgres" / slug / "native" / "bin"
        pkg.mkdir(parents=True, exist_ok=True)
        for name in binaries:
            binary = pkg / name
            binary.write_text("binary\n", encoding="utf-8")
            binary.chmod(0o755)
        if wrapper:
            wrapper_dir = modules / "embedded-postgres"
            wrapper_dir.mkdir(parents=True, exist_ok=True)
            (wrapper_dir / "package.json").write_text(
                '{"name":"embedded-postgres"}', encoding="utf-8"
            )

    def test_info_reports_missing_postgres_package(self):
        completed = run_launcher(["--launcher-info"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        info = json.loads(completed.stdout)
        postgres = info["embedded_postgres"]
        self.assertFalse(postgres["healthy"])
        self.assertFalse(postgres["present"])
        # The slug must still be reported so the user knows what to install.
        self.assertTrue(postgres["slug"])

    def test_info_reports_healthy_linux_postgres(self):
        # This sandbox is linux/x64, matching the slug the launcher will expect.
        self._add_native_postgres("linux-x64")
        completed = run_launcher(["--launcher-info"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        postgres = json.loads(completed.stdout)["embedded_postgres"]
        self.assertEqual(postgres["slug"], "linux-x64")
        self.assertTrue(postgres["present"], postgres["notes"])
        self.assertTrue(postgres["wrapper_present"])
        self.assertTrue(postgres["siblings_ok"])
        self.assertTrue(postgres["healthy"], postgres["notes"])
        self.assertEqual(postgres["missing_binaries"], [])

    def test_info_reports_missing_binary(self):
        self._add_native_postgres("linux-x64", binaries=("initdb", "postgres"))
        completed = run_launcher(["--launcher-info"], self._env())
        postgres = json.loads(completed.stdout)["embedded_postgres"]
        self.assertFalse(postgres["healthy"])
        self.assertIn("pg_ctl", postgres["missing_binaries"])

    def test_doctor_warns_but_does_not_block_on_incomplete_postgres(self):
        # Node + payload + node_modules are all fine, so the launcher must still
        # report "ready to run"; Postgres is a warning because Paperclip can use
        # an external DATABASE_URL instead.
        completed = run_launcher(["--launcher-doctor"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("ready to run", completed.stdout)
        self.assertIn("Embedded PostgreSQL", completed.stdout)
        self.assertIn("DATABASE_URL", completed.stdout)

    def test_doctor_reports_healthy_postgres(self):
        self._add_native_postgres("linux-x64")
        completed = run_launcher(["--launcher-doctor"], self._env())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("linux-x64", completed.stdout)
        self.assertIn("sibling layout  : correct", completed.stdout)
        self.assertNotIn("DATABASE_URL", completed.stdout)

    def test_linux_lib_path_is_exported_to_the_child(self):
        # The app sets LD_LIBRARY_PATH itself, but the launcher must guarantee it
        # for subprocesses spawned before that hook runs.
        lib = self.payload_root / "node_modules" / "@embedded-postgres" / "linux-x64" / "native" / "lib"
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "libpq.so.5.14").write_text("lib\n", encoding="utf-8")
        self._add_native_postgres("linux-x64")

        node_dir = self.tmp / "ldnode"
        node_dir.mkdir()
        probe = node_dir / "node"
        out = self.tmp / "ld.txt"
        probe.write_text(
            f'#!/bin/sh\ncase "$1" in --version) echo "{FAKE_NODE_VERSION}"; exit 0;; esac\n'
            f'echo "$LD_LIBRARY_PATH" > "{out}"\nexit 0\n',
            encoding="utf-8",
        )
        probe.chmod(0o755)

        env = self._env()
        env["PAPERCLIP_NODE"] = str(probe)
        completed = run_launcher(["doctor"], env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        exported = out.read_text(encoding="utf-8").strip()
        self.assertIn(str(lib), exported.split(os.pathsep))


if __name__ == "__main__":
    unittest.main()
