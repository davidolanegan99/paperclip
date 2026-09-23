"""Tests for payload discovery and the user data directory."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe._platform import LINUX, MACOS, WINDOWS, X64, platform_from_keys  # noqa: E402
from paperclip_exe import payload as payload_module  # noqa: E402
from paperclip_exe.payload import (  # noqa: E402
    Payload,
    PayloadError,
    data_dir,
    find_payload,
    payload_manifest,
)


def make_payload(root: Path, *, with_node_modules: bool = True, version: str = "0.3.1") -> Path:
    """Create a minimal but realistic payload tree; returns the entry path."""
    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    entry = app / "index.js"
    entry.write_text("console.log('paperclip');\n", encoding="utf-8")
    (app / "package.json").write_text(
        json.dumps({"name": "paperclipai", "version": version}), encoding="utf-8"
    )
    if with_node_modules:
        modules = root / "node_modules" / "commander"
        modules.mkdir(parents=True, exist_ok=True)
        (modules / "package.json").write_text('{"name":"commander"}', encoding="utf-8")
    return entry


class EnvGuard:
    """Temporarily set/clear payload-related environment variables."""

    KEYS = ("PAPERCLIP_PAYLOAD", "PAPERCLIP_ENTRY", "PAPERCLIP_DATA_DIR")

    def __enter__(self):
        self.saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class FindPayloadTests(unittest.TestCase):
    def test_finds_payload_via_env_root(self):
        with TemporaryDirectory() as tmp, EnvGuard():
            root = Path(tmp) / "payload"
            entry = make_payload(root)
            os.environ["PAPERCLIP_PAYLOAD"] = str(root)
            found = find_payload()
            self.assertEqual(found.entry, entry)
            self.assertIsNotNone(found.node_modules)
            self.assertEqual(found.cwd, entry.parent)

    def test_finds_payload_via_env_entry(self):
        with TemporaryDirectory() as tmp, EnvGuard():
            root = Path(tmp) / "payload"
            entry = make_payload(root)
            os.environ["PAPERCLIP_ENTRY"] = str(entry)
            found = find_payload()
            self.assertEqual(found.entry, entry)
            self.assertEqual(found.source, "PAPERCLIP_ENTRY")

    def test_entry_env_pointing_at_missing_file_raises(self):
        with TemporaryDirectory() as tmp, EnvGuard():
            os.environ["PAPERCLIP_ENTRY"] = str(Path(tmp) / "nope.js")
            with self.assertRaises(PayloadError) as ctx:
                find_payload()
            self.assertIn("missing file", str(ctx.exception))

    def test_discovers_cli_dist_layout(self):
        # The repo layout (cli/dist/index.js) must be found without staging,
        # so developers can point the exe straight at a working tree.
        with TemporaryDirectory() as tmp, EnvGuard():
            root = Path(tmp) / "payload"
            dist = root / "cli" / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.js").write_text("// bundle\n", encoding="utf-8")
            os.environ["PAPERCLIP_PAYLOAD"] = str(root)
            found = find_payload()
            self.assertEqual(found.entry, dist / "index.js")

    def test_missing_payload_error_is_actionable(self):
        with TemporaryDirectory() as tmp, EnvGuard():
            os.environ["PAPERCLIP_PAYLOAD"] = str(Path(tmp) / "empty")
            os.chdir(tmp)
            with self.assertRaises(PayloadError) as ctx:
                find_payload()
        message = str(ctx.exception)
        self.assertIn("app/index.js", message)
        self.assertIn("PAPERCLIP_PAYLOAD", message)
        self.assertIn("PAPERCLIP_ENTRY", message)


class PayloadManifestTests(unittest.TestCase):
    def test_manifest_reports_app_version(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "payload"
            entry = make_payload(root, version="9.9.9")
            payload = Payload(
                root=root,
                entry=entry,
                node_modules=root / "node_modules",
                assets=[],
                source="test",
            )
            manifest = payload_manifest(payload)
            self.assertEqual(manifest["app_version"], "9.9.9")
            self.assertEqual(manifest["entry"], str(entry))

    def test_manifest_tolerates_missing_package_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "app" / "index.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("//\n", encoding="utf-8")
            payload = Payload(root=root, entry=entry, node_modules=None, assets=[], source="test")
            self.assertIsNone(payload_manifest(payload)["app_version"])

    def test_manifest_tolerates_corrupt_package_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir(parents=True)
            (app / "index.js").write_text("//\n", encoding="utf-8")
            (app / "package.json").write_text("{not json", encoding="utf-8")
            payload = Payload(root=root, entry=app / "index.js", node_modules=None, assets=[], source="test")
            self.assertIsNone(payload_manifest(payload)["app_version"])


class DataDirTests(unittest.TestCase):
    def test_env_override_wins(self):
        with TemporaryDirectory() as tmp, EnvGuard():
            os.environ["PAPERCLIP_DATA_DIR"] = str(tmp)
            self.assertEqual(data_dir(), Path(tmp))

    def test_platform_defaults(self):
        import paperclip_exe._platform as plat_module

        # (os, expected fragment, expected leaf) with platform env vars unset so
        # we assert the documented fallback locations.
        cases = [
            (WINDOWS, "AppData/Local", "paperclip"),
            (MACOS, "Library/Application Support", "paperclip"),
            (LINUX, ".local/share", "paperclip"),
        ]
        for os_key, fragment, leaf in cases:
            with self.subTest(os=os_key):
                original = plat_module.current_platform
                try:
                    plat_module.current_platform = lambda os_key=os_key: platform_from_keys(os_key, X64)
                    payload_module.current_platform = plat_module.current_platform
                    with EnvGuard():
                        for key in ("LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME"):
                            os.environ.pop(key, None)
                        resolved = data_dir()
                        self.assertEqual(resolved.name, leaf)
                        self.assertIn(fragment, resolved.as_posix())
                        # Must stay anchored in the user's home directory.
                        self.assertIn(str(Path.home()), str(resolved))
                finally:
                    plat_module.current_platform = original
                    payload_module.current_platform = original

    def test_platform_env_vars_take_precedence_over_home(self):
        import paperclip_exe._platform as plat_module

        with TemporaryDirectory() as tmp:
            cases = [
                (WINDOWS, "LOCALAPPDATA", Path(tmp) / "LocalAppData"),
                (LINUX, "XDG_DATA_HOME", Path(tmp) / "xdg"),
            ]
            for os_key, var, base in cases:
                with self.subTest(os=os_key, var=var):
                    base.mkdir(parents=True, exist_ok=True)
                    original = plat_module.current_platform
                    try:
                        plat_module.current_platform = lambda os_key=os_key: platform_from_keys(os_key, X64)
                        payload_module.current_platform = plat_module.current_platform
                        with EnvGuard():
                            os.environ[var] = str(base)
                            self.assertEqual(data_dir(), base / "paperclip")
                    finally:
                        plat_module.current_platform = original
                        payload_module.current_platform = original

    def test_windows_uses_localappdata_when_set(self):
        import paperclip_exe._platform as plat_module

        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "AppData" / "Local"
            base.mkdir(parents=True)
            original = plat_module.current_platform
            try:
                plat_module.current_platform = lambda: platform_from_keys(WINDOWS, X64)
                payload_module.current_platform = plat_module.current_platform
                with EnvGuard():
                    os.environ["LOCALAPPDATA"] = str(base)
                    self.assertEqual(data_dir(), base / "paperclip")
            finally:
                plat_module.current_platform = original
                payload_module.current_platform = original


if __name__ == "__main__":
    unittest.main()
