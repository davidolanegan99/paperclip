"""Tests for paperclip_exe.runtime -- version parsing and Node discovery."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe._platform import LINUX, MACOS, WINDOWS, X64, platform_from_keys  # noqa: E402
from paperclip_exe.runtime import (  # noqa: E402
    NodeRuntime,
    NodeRuntimeError,
    NodeVersion,
    _strip_top_level,
    extract_node_archive,
    node_dist_url,
    probe_node,
    resolve_node,
)


def make_fake_node(directory: Path, version: str, *, name: str = "node", fail: bool = False) -> Path:
    """Create an executable that mimics ``node --version``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if fail:
        script = "#!/bin/sh\necho 'broken' >&2\nexit 1\n"
    else:
        script = f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\nexit 0\n'
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class NodeVersionTests(unittest.TestCase):
    def test_parses_plain_and_prefixed_versions(self):
        self.assertEqual(NodeVersion.parse("24.11.0"), NodeVersion(24, 11, 0))
        self.assertEqual(NodeVersion.parse("v24.11.0"), NodeVersion(24, 11, 0))

    def test_parses_version_from_noisy_output(self):
        self.assertEqual(NodeVersion.parse("node v22.5.1\n"), NodeVersion(22, 5, 1))

    def test_rejects_unparseable_text(self):
        with self.assertRaises(NodeRuntimeError):
            NodeVersion.parse("not a version")
        with self.assertRaises(NodeRuntimeError):
            NodeVersion.parse("")

    def test_minimum_version_gate(self):
        self.assertTrue(NodeVersion.parse("24.11.0").satisfies_minimum("24.11.0"))
        self.assertTrue(NodeVersion.parse("25.0.0").satisfies_minimum("24.11.0"))
        self.assertTrue(NodeVersion.parse("24.12.0").satisfies_minimum("24.11.0"))
        self.assertFalse(NodeVersion.parse("24.10.9").satisfies_minimum("24.11.0"))
        self.assertFalse(NodeVersion.parse("22.22.3").satisfies_minimum("24.11.0"))

    def test_str_roundtrip(self):
        self.assertEqual(str(NodeVersion(24, 11, 0)), "24.11.0")


class ProbeNodeTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "fake node script needs a POSIX shell")
    def test_probe_returns_version_for_working_node(self):
        with TemporaryDirectory() as tmp:
            path = make_fake_node(Path(tmp), "v24.11.0")
            self.assertEqual(probe_node(path), NodeVersion(24, 11, 0))

    @unittest.skipIf(os.name == "nt", "fake node script needs a POSIX shell")
    def test_probe_returns_none_for_failing_node(self):
        with TemporaryDirectory() as tmp:
            path = make_fake_node(Path(tmp), "v24.11.0", fail=True)
            self.assertIsNone(probe_node(path))

    def test_probe_returns_none_for_missing_path(self):
        self.assertIsNone(probe_node(Path("/definitely/not/here/node")))

    def test_probe_returns_none_for_directory(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(probe_node(Path(tmp)))


class ResolveNodeTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "fake node script needs a POSIX shell")
    def test_env_override_wins_over_path(self):
        with TemporaryDirectory() as tmp:
            override = make_fake_node(Path(tmp) / "override", "v24.11.0")
            make_fake_node(Path(tmp) / "onpath", "v24.11.0", name="node")
            old_path = os.environ.get("PATH")
            old_override = os.environ.get("PAPERCLIP_NODE")
            try:
                os.environ["PAPERCLIP_NODE"] = str(override)
                os.environ["PATH"] = str(Path(tmp) / "onpath")
                runtime = resolve_node(plat=platform_from_keys(LINUX, X64))
                self.assertEqual(runtime.path, override)
                self.assertEqual(runtime.source, "PAPERCLIP_NODE")
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
                if old_override is None:
                    os.environ.pop("PAPERCLIP_NODE", None)
                else:
                    os.environ["PAPERCLIP_NODE"] = old_override

    @unittest.skipIf(os.name == "nt", "fake node script needs a POSIX shell")
    def test_rejects_node_below_minimum_with_actionable_error(self):
        with TemporaryDirectory() as tmp:
            make_fake_node(Path(tmp), "v22.22.3")
            old_path = os.environ.get("PATH")
            old_override = os.environ.pop("PAPERCLIP_NODE", None)
            try:
                os.environ["PATH"] = str(tmp)
                with self.assertRaises(NodeRuntimeError) as ctx:
                    resolve_node(
                        plat=platform_from_keys(LINUX, X64),
                        minimum="24.11.0",
                        cache_dir=Path(tmp) / "empty-cache",
                    )
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
                if old_override is not None:
                    os.environ["PAPERCLIP_NODE"] = old_override
        message = str(ctx.exception)
        # The error must tell the user how to fix it, not just that it failed.
        self.assertIn("24.11.0", message)
        self.assertIn("PAPERCLIP_NODE", message)
        self.assertIn("nodejs.org", message)

    @unittest.skipIf(os.name == "nt", "fake node script needs a POSIX shell")
    def test_finds_node_from_cache_dir(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            make_fake_node(cache / "bin", "v24.11.0")
            old_override = os.environ.pop("PAPERCLIP_NODE", None)
            old_path = os.environ.get("PATH")
            try:
                os.environ["PATH"] = str(Path(tmp) / "nothing-here")
                runtime = resolve_node(
                    plat=platform_from_keys(LINUX, X64),
                    cache_dir=cache,
                )
                self.assertEqual(runtime.version, NodeVersion(24, 11, 0))
                self.assertIn("cached", runtime.source)
            finally:
                if old_override is not None:
                    os.environ["PAPERCLIP_NODE"] = old_override
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path


class InstallPortableNodeTests(unittest.TestCase):
    """Verify the install path without touching the network.

    ``install_portable_node`` must reuse an existing valid install rather than
    re-downloading, and must detect a broken install and refuse to trust it.
    """

    @unittest.skipIf(os.name == "nt", "POSIX fake node script")
    def test_reuses_existing_valid_install_without_downloading(self):
        from paperclip_exe.runtime import install_portable_node

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "node"
            make_fake_node(destination / "bin", "v24.11.0")

            # Point the mirror at a dead port: any download attempt fails loudly.
            import paperclip_exe.runtime as runtime_module

            original = runtime_module.NODE_DIST_MIRROR
            try:
                runtime_module.NODE_DIST_MIRROR = "http://127.0.0.1:1"
                resolved = install_portable_node(
                    destination, plat=platform_from_keys(LINUX, X64), version="v24.11.0"
                )
            finally:
                runtime_module.NODE_DIST_MIRROR = original

            self.assertEqual(resolved, destination / "bin" / "node")

    @unittest.skipIf(os.name == "nt", "POSIX fake node script")
    def test_reports_failure_when_download_unreachable(self):
        from paperclip_exe.runtime import install_portable_node

        with TemporaryDirectory() as tmp:
            import paperclip_exe.runtime as runtime_module

            original = runtime_module.NODE_DIST_MIRROR
            try:
                runtime_module.NODE_DIST_MIRROR = "http://127.0.0.1:1"
                with self.assertRaises(NodeRuntimeError) as ctx:
                    install_portable_node(
                        Path(tmp) / "node",
                        plat=platform_from_keys(LINUX, X64),
                        version="v24.11.0",
                    )
            finally:
                runtime_module.NODE_DIST_MIRROR = original
            self.assertIn("download failed", str(ctx.exception))


class NodeDistUrlTests(unittest.TestCase):
    def test_windows_url(self):
        url = node_dist_url("v24.11.0", platform_from_keys(WINDOWS, X64))
        self.assertIn("v24.11.0/node-v24.11.0-win-x64.zip", url)

    def test_macos_arm64_url(self):
        url = node_dist_url("24.11.0", platform_from_keys(MACOS, "arm64"))
        self.assertIn("v24.11.0/node-v24.11.0-darwin-arm64.tar.gz", url)

    def test_linux_x64_url(self):
        url = node_dist_url("v24.11.0", platform_from_keys(LINUX, X64))
        self.assertIn("v24.11.0/node-v24.11.0-linux-x64.tar.xz", url)

    def test_version_prefix_is_normalised(self):
        with_prefix = node_dist_url("v24.11.0", platform_from_keys(LINUX, X64))
        without_prefix = node_dist_url("24.11.0", platform_from_keys(LINUX, X64))
        self.assertEqual(with_prefix, without_prefix)


class ExtractNodeArchiveTests(unittest.TestCase):
    def _build_tarball(self, root_name: str, plat) -> Path:
        import tarfile

        tmp = Path(self.tmp.name)
        stage = tmp / root_name
        bin_dir = stage / "bin" if plat.os != WINDOWS else stage
        bin_dir.mkdir(parents=True, exist_ok=True)
        node_bin = bin_dir / (f"node{plat.exe_suffix}")
        node_bin.write_text("#!/bin/sh\necho v24.11.0\n", encoding="utf-8")
        node_bin.chmod(0o755)

        archive = tmp / f"{root_name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(stage, arcname=root_name)
        return archive

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = self._tmp

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX tarball layout")
    def test_strips_top_level_tarball_directory(self):
        plat = platform_from_keys(LINUX, X64)
        archive = self._build_tarball("node-v24.11.0-linux-x64", plat)
        destination = Path(self.tmp.name) / "out"
        root = extract_node_archive(archive, destination, plat)
        # The layout is normalised so callers can rely on destination/bin/node.
        self.assertEqual(root, destination)
        self.assertTrue((destination / "bin" / "node").is_file())
        self.assertFalse((destination / "node-v24.11.0-linux-x64").exists())

    @unittest.skipIf(os.name == "nt", "POSIX tarball layout")
    def test_extraction_is_idempotent_and_does_not_nest(self):
        # Regression: a nested node-vX/ directory made install_portable_node
        # re-download Node on every single run.
        plat = platform_from_keys(LINUX, X64)
        archive = self._build_tarball("node-v24.11.0-linux-x64", plat)
        destination = Path(self.tmp.name) / "idem"
        extract_node_archive(archive, destination, plat)
        extract_node_archive(archive, destination, plat)
        self.assertTrue((destination / "bin" / "node").is_file())
        nested = [p for p in destination.iterdir() if p.is_dir() and p.name.startswith("node-v")]
        self.assertEqual(nested, [])

    def test_strip_top_level_returns_none_for_multiple_entries(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            self.assertIsNone(_strip_top_level(root))

    def test_strip_top_level_returns_none_for_flat_layout(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node").write_text("x", encoding="utf-8")
            self.assertIsNone(_strip_top_level(root))

    def test_unsupported_archive_format_raises(self):
        with TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "node.rar"
            bogus.write_text("nope", encoding="utf-8")
            with self.assertRaises(NodeRuntimeError):
                extract_node_archive(bogus, Path(tmp) / "out", platform_from_keys(LINUX, X64))


if __name__ == "__main__":
    unittest.main()
