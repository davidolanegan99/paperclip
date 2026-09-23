"""Tests for the frozen-executable code path.

A real ``paperclip.exe`` runs with ``sys.frozen`` set and ``sys._MEIPASS``
pointing at PyInstaller's extraction directory. These tests simulate that state
against a bundle laid out exactly like ``paperclip_exe.spec`` produces, so the
resolution logic the exe depends on is covered without needing to freeze a
binary (which requires a shared-libpython interpreter).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe import payload as payload_module  # noqa: E402
from paperclip_exe import runtime as runtime_module  # noqa: E402
from paperclip_exe.payload import find_payload  # noqa: E402
from paperclip_exe.runtime import resolve_node  # noqa: E402

FAKE_NODE_VERSION = "v24.11.0"


def make_bundle(root: Path) -> Path:
    """Build a directory tree mirroring the frozen ``_MEIPASS`` layout."""
    # payload/app/index.js + package.json
    app = root / "payload" / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "index.js").write_text("// bundle\n", encoding="utf-8")
    (app / "package.json").write_text(
        json.dumps({"name": "paperclipai", "version": "0.3.1-frozen"}), encoding="utf-8"
    )
    # payload/node_modules/<dep>
    dep = root / "payload" / "node_modules" / "commander"
    dep.mkdir(parents=True, exist_ok=True)
    (dep / "package.json").write_text('{"name":"commander"}', encoding="utf-8")
    # runtime/node/bin/node (POSIX) or runtime/node/node.exe (Windows)
    node_bin = root / "runtime" / "node" / "bin" / "node"
    node_bin.parent.mkdir(parents=True, exist_ok=True)
    node_bin.write_text(
        f'#!/bin/sh\ncase "$1" in --version) echo "{FAKE_NODE_VERSION}"; exit 0;; esac\nexit 0\n',
        encoding="utf-8",
    )
    node_bin.chmod(node_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return root


class FrozenSimulation:
    """Context manager that makes the launcher believe it is frozen."""

    def __init__(self, meipass: Path, executable: Path):
        self.meipass = meipass
        self.executable = executable
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "frozen": getattr(sys, "frozen", None),
            "_MEIPASS": getattr(sys, "_MEIPASS", None),
            "executable": sys.executable,
            "env": {k: os.environ.get(k) for k in (
                "PAPERCLIP_NODE", "PAPERCLIP_PAYLOAD", "PAPERCLIP_ENTRY",
                "PAPERCLIP_STRICT_PAYLOAD", "PAPERCLIP_FETCH_NODE",
            )},
        }
        for key in self._saved["env"]:
            os.environ.pop(key, None)
        sys.frozen = True
        sys._MEIPASS = str(self.meipass)
        sys.executable = str(self.executable)
        return self

    def __exit__(self, *exc):
        for attr in ("frozen", "_MEIPASS"):
            value = self._saved[attr]
            if value is None:
                if hasattr(sys, attr):
                    delattr(sys, attr)
            else:
                setattr(sys, attr, value)
        sys.executable = self._saved["executable"]
        for key, value in self._saved["env"].items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


@unittest.skipIf(os.name == "nt", "fake node is a POSIX shell script")
class FrozenBundleResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.meipass = self.tmp / "_MEIPASS"
        make_bundle(self.meipass)
        # PyInstaller onefile puts the exe outside _MEIPASS.
        self.exe = self.tmp / "paperclip"
        self.exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.exe.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_payload_inside_the_bundle(self):
        with FrozenSimulation(self.meipass, self.exe):
            found = find_payload()
        self.assertEqual(found.entry, self.meipass / "payload" / "app" / "index.js")
        self.assertEqual(
            found.node_modules, self.meipass / "payload" / "node_modules"
        )
        self.assertEqual(found.cwd, self.meipass / "payload" / "app")

    def test_finds_embedded_node_runtime(self):
        from paperclip_exe._platform import LINUX, X64, platform_from_keys

        with FrozenSimulation(self.meipass, self.exe):
            resolved = resolve_node(plat=platform_from_keys(LINUX, X64))
        self.assertEqual(resolved.path, self.meipass / "runtime" / "node" / "bin" / "node")
        self.assertIn("bundled", resolved.source)
        self.assertEqual(str(resolved.version), "24.11.0")

    def test_frozen_search_ignores_source_tree(self):
        # A shipped exe must never pick up a developer checkout: with the cwd
        # pointing at the repo, the bundled payload must still win.
        original_cwd = Path.cwd()
        try:
            os.chdir(HERE)
            with FrozenSimulation(self.meipass, self.exe):
                found = find_payload()
            self.assertTrue(str(found.root).startswith(str(self.meipass)))
        finally:
            os.chdir(original_cwd)

    def test_frozen_falls_back_to_cwd_when_bundle_empty(self):
        # Supports dropping an updated payload beside the exe / in the cwd.
        empty_meipass = self.tmp / "empty_meipass"
        empty_meipass.mkdir()
        cwd_payload = self.tmp / "cwdpayload"
        make_bundle(cwd_payload)
        original_cwd = Path.cwd()
        try:
            os.chdir(cwd_payload)
            with FrozenSimulation(empty_meipass, self.exe):
                found = find_payload()
            self.assertEqual(found.entry, cwd_payload / "payload" / "app" / "index.js")
        finally:
            os.chdir(original_cwd)

    def test_bundled_root_reports_meipass_when_frozen(self):
        from paperclip_exe._platform import bundled_root

        with FrozenSimulation(self.meipass, self.exe):
            self.assertEqual(bundled_root(), self.meipass)


if __name__ == "__main__":
    unittest.main()
