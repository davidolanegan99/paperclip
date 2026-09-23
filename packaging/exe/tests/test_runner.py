"""Tests for process spawning: env construction, argv order, exit propagation."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe.runner import (  # noqa: E402
    LAUNCHER_ERROR_EXIT,
    build_command,
    build_environment,
    exit_code_from_returncode,
    run_app,
)


class BuildEnvironmentTests(unittest.TestCase):
    def test_prepends_node_dir_to_path(self):
        with TemporaryDirectory() as tmp:
            node_dir = Path(tmp) / "nodebin"
            node_dir.mkdir()
            node = node_dir / "node"
            env = build_environment(node, None)
            parts = env["PATH"].split(os.pathsep)
            self.assertEqual(parts[0], str(node_dir))

    def test_does_not_duplicate_node_dir_in_path(self):
        with TemporaryDirectory() as tmp:
            node_dir = Path(tmp) / "nodebin"
            node_dir.mkdir()
            node = node_dir / "node"
            os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
            env = build_environment(node, None)
            parts = env["PATH"].split(os.pathsep)
            self.assertEqual(parts.count(str(node_dir)), 1)

    def test_sets_node_path_for_payload_modules(self):
        with TemporaryDirectory() as tmp:
            node = Path(tmp) / "node"
            modules = Path(tmp) / "node_modules"
            env = build_environment(node, modules)
            self.assertIn(str(modules), env["NODE_PATH"].split(os.pathsep))

    def test_omits_node_path_when_no_modules(self):
        with TemporaryDirectory() as tmp:
            node = Path(tmp) / "node"
            os.environ.pop("NODE_PATH", None)
            env = build_environment(node, None)
            self.assertEqual(env.get("NODE_PATH", ""), "")

    def test_marks_child_as_launched(self):
        with TemporaryDirectory() as tmp:
            env = build_environment(Path(tmp) / "node", None)
            self.assertEqual(env["PAPERCLIP_EXE_LAUNCHER"], "1")
            self.assertIn("PAPERCLIP_LAUNCHER_VERSION", env)

    def test_inherits_existing_environment(self):
        with TemporaryDirectory() as tmp:
            os.environ["PAPERCLIP_TEST_INHERITED"] = "yes"
            try:
                env = build_environment(Path(tmp) / "node", None)
                self.assertEqual(env["PAPERCLIP_TEST_INHERITED"], "yes")
            finally:
                os.environ.pop("PAPERCLIP_TEST_INHERITED", None)

    def test_extra_values_override_and_none_is_ignored(self):
        with TemporaryDirectory() as tmp:
            env = build_environment(Path(tmp) / "node", None, extra={"FOO": "bar", "SKIP": None})
            self.assertEqual(env["FOO"], "bar")
            self.assertNotIn("SKIP", env)


class BuildCommandTests(unittest.TestCase):
    def test_order_is_node_options_entry_appargs(self):
        command = build_command(
            Path("/opt/node"),
            Path("/app/index.js"),
            ["issue", "list"],
            node_options=["--max-old-space-size=4096"],
        )
        self.assertEqual(
            command,
            ["/opt/node", "--max-old-space-size=4096", "/app/index.js", "issue", "list"],
        )

    def test_no_node_options_by_default(self):
        command = build_command(Path("/opt/node"), Path("/app/index.js"), ["doctor"])
        self.assertEqual(command, ["/opt/node", "/app/index.js", "doctor"])

    def test_empty_app_args_still_targets_entry(self):
        command = build_command(Path("/opt/node"), Path("/app/index.js"), [])
        self.assertEqual(command[-1], "/app/index.js")


class ExitCodeTests(unittest.TestCase):
    def test_positive_codes_pass_through(self):
        for code in (0, 1, 2, 78, 255):
            self.assertEqual(exit_code_from_returncode(code), code)

    def test_signal_deaths_use_posix_convention(self):
        self.assertEqual(exit_code_from_returncode(-2), 130)  # SIGINT
        self.assertEqual(exit_code_from_returncode(-9), 137)  # SIGKILL
        self.assertEqual(exit_code_from_returncode(-15), 143)  # SIGTERM


@unittest.skipIf(os.name == "nt", "uses POSIX shell fake apps")
class RunAppTests(unittest.TestCase):
    """run_app must behave like a transparent exec of the real app."""

    def _fake(self, tmp: Path, script: str) -> Path:
        path = tmp / "fake-node"
        path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_exit_code_is_propagated(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = self._fake(tmp_path, "exit 42\n")
            entry = tmp_path / "app.js"
            entry.write_text("//\n", encoding="utf-8")
            code = run_app([str(fake), str(entry)], os.environ, tmp_path)
            self.assertEqual(code, 42)

    def test_success_returns_zero(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = self._fake(tmp_path, "exit 0\n")
            code = run_app([str(fake), "app.js"], os.environ, tmp_path)
            self.assertEqual(code, 0)

    def test_arguments_are_forwarded_in_order(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "args.txt"
            fake = self._fake(tmp_path, f'printf "%s\\n" "$@" > "{out}"\nexit 0\n')
            entry = tmp_path / "app.js"
            entry.write_text("//\n", encoding="utf-8")
            run_app(
                [str(fake), str(entry), "issue", "list", "--company", "acme"],
                os.environ,
                tmp_path,
            )
            forwarded = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(forwarded, [str(entry), "issue", "list", "--company", "acme"])

    def test_environment_reaches_the_child(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "env.txt"
            fake = self._fake(tmp_path, f'echo "$PAPERCLIP_EXE_LAUNCHER" > "{out}"\nexit 0\n')
            env = dict(os.environ)
            env["PAPERCLIP_EXE_LAUNCHER"] = "1"
            run_app([str(fake), "app.js"], env, tmp_path)
            self.assertEqual(out.read_text(encoding="utf-8").strip(), "1")

    def test_child_output_is_inherited_not_captured(self):
        # stdio inheritance is what makes interactive prompts work; assert we do
        # not redirect the child's streams by running this test's own subprocess.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = HERE / "tests" / "_stdio_probe.py"
            completed = subprocess.run(
                [sys.executable, str(script)], capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("STDOUT_OK", completed.stdout)
            self.assertIn("STDERR_OK", completed.stderr)

    def test_missing_binary_returns_config_error(self):
        with TemporaryDirectory() as tmp:
            code = run_app([str(Path(tmp) / "absent-node"), "app.js"], os.environ, Path(tmp))
            self.assertEqual(code, LAUNCHER_ERROR_EXIT)


if __name__ == "__main__":
    unittest.main()
