"""Tests for dependency-tree materialization.

These cover the two hazards that make a pnpm checkout unbundleable:

* symlinks -- Windows cannot create them without privilege, and PyInstaller
  archives do not preserve them, so every link must become a real file;
* the ``embedded-postgres`` / ``@embedded-postgres/<slug>`` **sibling** layout,
  which the app resolves with ``path.resolve(packageRoot, "..", ...)``.
"""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe._platform import WINDOWS, X64, platform_from_keys  # noqa: E402
from paperclip_exe.postgres import NATIVE_SCOPE, WRAPPER_PACKAGE, inspect  # noqa: E402
from paperclip_exe.staging import (  # noqa: E402
    StagingError,
    copy_tree_materialized,
    count_symlinks,
    ensure_executable_bits,
    merge_trees,
)

WIN = platform_from_keys(WINDOWS, X64)

#: Skip the symlink-specific tests where the OS cannot create them unprivileged.
can_symlink = hasattr(os, "symlink")


def _try_symlink(target: Path, link: Path) -> bool:
    try:
        os.symlink(str(target), str(link))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def build_pnpm_style_store(root: Path) -> Path:
    """Recreate the pnpm layout that breaks naive copying.

    ``node_modules/<pkg>`` is a symlink into ``.pnpm/<pkg>@v/node_modules/<pkg>``,
    and each package's own dependencies are symlinks too -- siblings within the
    virtual store directory.
    """
    store = root / "node_modules" / ".pnpm"

    # embedded-postgres wrapper, with @embedded-postgres/windows-x64 as a sibling
    # inside the same virtual-store node_modules (this is what `..` resolves to).
    wrapper_store = store / "embedded-postgres@18.1.0" / "node_modules"
    wrapper_real = wrapper_store / WRAPPER_PACKAGE / "dist"
    wrapper_real.mkdir(parents=True)
    (wrapper_real / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (wrapper_store / WRAPPER_PACKAGE / "package.json").write_text(
        '{"name":"embedded-postgres","version":"18.1.0"}', encoding="utf-8"
    )

    native_real = wrapper_store / NATIVE_SCOPE / "windows-x64" / "native" / "bin"
    native_real.mkdir(parents=True)
    for name in ("initdb", "postgres", "pg_ctl"):
        binary = native_real / f"{name}.exe"
        binary.write_text("MZ fake\n", encoding="utf-8")
        binary.chmod(0o755)
    (wrapper_store / NATIVE_SCOPE / "windows-x64" / "package.json").write_text(
        '{"name":"@embedded-postgres/windows-x64"}', encoding="utf-8"
    )

    # A second, unrelated package to prove general materialization.
    commander_real = store / "commander@15.0.0" / "node_modules" / "commander"
    commander_real.mkdir(parents=True)
    (commander_real / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (commander_real / "package.json").write_text('{"name":"commander"}', encoding="utf-8")

    # Top-level node_modules holds symlinks into the store.
    top = root / "node_modules"
    top.mkdir(parents=True, exist_ok=True)
    links = [
        (wrapper_store / WRAPPER_PACKAGE, top / WRAPPER_PACKAGE),
        (store / "commander@15.0.0" / "node_modules" / "commander", top / "commander"),
        # The scoped native package is also linked at the top level.
        (wrapper_store / NATIVE_SCOPE / "windows-x64", top / NATIVE_SCOPE / "windows-x64"),
    ]
    made = 0
    for target, link in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if _try_symlink(target, link):
            made += 1
    return top if made else top


@unittest.skipUnless(can_symlink, "os.symlink unavailable")
class MaterializationTests(unittest.TestCase):
    def test_symlinks_become_real_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "real").mkdir(parents=True)
            (src / "real" / "file.txt").write_text("content\n", encoding="utf-8")
            target = src / "real"
            link = src / "link"
            if not _try_symlink(target, link):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)

            self.assertEqual(stats.symlinks_dereferenced, 1)
            self.assertTrue((dst / "link" / "file.txt").is_file())
            self.assertFalse((dst / "link").is_symlink())
            self.assertEqual(count_symlinks(dst), 0)

    def test_broken_symlinks_are_skipped_not_fatal(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "good.txt").write_text("ok\n", encoding="utf-8")
            dangling = src / "dangling"
            if not _try_symlink(root / "does-not-exist", dangling):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)

            self.assertTrue((dst / "good.txt").is_file())
            self.assertFalse((dst / "dangling").exists())
            self.assertEqual(stats.broken_symlinks_skipped, 1)

    def test_symlink_loop_terminates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "file.txt").write_text("ok\n", encoding="utf-8")
            if not _try_symlink(src, src / "loop"):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)  # must not hang or recurse forever

            self.assertTrue((dst / "file.txt").is_file())
            self.assertGreaterEqual(stats.loops_skipped, 1)

    def test_executable_bit_is_preserved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            binary = src / "postgres"
            binary.write_text("MZ\n", encoding="utf-8")
            binary.chmod(0o755)
            plain = src / "readme.txt"
            plain.write_text("hi\n", encoding="utf-8")
            plain.chmod(0o644)

            dst = root / "dst"
            copy_tree_materialized(src, dst)

            self.assertTrue(dst.joinpath("postgres").stat().st_mode & stat.S_IXUSR)
            self.assertFalse(dst.joinpath("readme.txt").stat().st_mode & stat.S_IXUSR)

    def test_noise_directories_are_skipped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "__pycache__").mkdir(parents=True)
            (src / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
            (src / ".git").mkdir()
            (src / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            (src / "keep.js").write_text("//\n", encoding="utf-8")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)

            self.assertTrue((dst / "keep.js").is_file())
            self.assertFalse((dst / "__pycache__").exists())
            self.assertFalse((dst / ".git").exists())
            self.assertGreaterEqual(stats.skipped_by_rule, 2)

    def test_missing_source_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(StagingError):
                copy_tree_materialized(Path(tmp) / "absent", Path(tmp) / "dst")

    def test_destination_name_comes_from_the_link_not_the_target(self):
        """Regression: symlinked dirs were renamed to their target's basename.

        pnpm links ``node_modules/foo`` at ``.pnpm/foo@1.2.3/node_modules/foo``;
        a shared store dir can also be linked under a *different* name. Using the
        resolved basename silently renamed entries and broke resolution.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            store = root / "store" / "real-pkg@1.0.0"
            store.mkdir(parents=True)
            (store / "index.js").write_text("module.exports=1;\n", encoding="utf-8")
            src.mkdir()
            if not _try_symlink(store, src / "aliased-name"):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            copy_tree_materialized(src, dst)

            # Must land under the link's name, never "real-pkg@1.0.0".
            self.assertTrue((dst / "aliased-name" / "index.js").is_file())
            self.assertFalse((dst / "real-pkg@1.0.0").exists())

    def test_shared_directory_in_sibling_branches_is_not_treated_as_a_loop(self):
        """Regression: global loop detection dropped pnpm's shared store dirs.

        The same real directory is legitimately reachable from several symlinks
        in *different* subtrees. Only a directory that is its own ancestor is a
        loop, so the seen-set must be branch-scoped.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            shared = root / "shared"
            shared.mkdir()
            (shared / "data.txt").write_text("shared\n", encoding="utf-8")

            (src / "a").mkdir(parents=True)
            (src / "b").mkdir(parents=True)
            if not _try_symlink(shared, src / "a" / "link") or not _try_symlink(
                shared, src / "b" / "link"
            ):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)

            # Both branches must be materialized.
            self.assertTrue((dst / "a" / "link" / "data.txt").is_file())
            self.assertTrue((dst / "b" / "link" / "data.txt").is_file())
            self.assertEqual(stats.loops_skipped, 0)

    def test_true_self_referential_loop_is_still_detected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            nested = src / "nested"
            nested.mkdir(parents=True)
            (nested / "file.txt").write_text("ok\n", encoding="utf-8")
            # nested/self -> src, making src its own ancestor.
            if not _try_symlink(src, nested / "self"):
                self.skipTest("cannot create symlinks in this environment")

            dst = root / "dst"
            stats = copy_tree_materialized(src, dst)

            self.assertTrue((dst / "nested" / "file.txt").is_file())
            self.assertGreaterEqual(stats.loops_skipped, 1)


@unittest.skipUnless(can_symlink, "os.symlink unavailable")
class MergeTreesTests(unittest.TestCase):
    def test_first_root_wins_and_gaps_are_filled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "cli-node-modules"
            secondary = root / "root-node-modules"
            (primary / "commander").mkdir(parents=True)
            (primary / "commander" / "package.json").write_text('{"from":"primary"}', encoding="utf-8")
            (secondary / "commander").mkdir(parents=True)
            (secondary / "commander" / "package.json").write_text('{"from":"secondary"}', encoding="utf-8")
            (secondary / "dotenv").mkdir(parents=True)
            (secondary / "dotenv" / "package.json").write_text('{"from":"secondary"}', encoding="utf-8")

            dst = root / "out"
            merge_trees([primary, secondary], dst)

            self.assertIn("primary", (dst / "commander" / "package.json").read_text(encoding="utf-8"))
            self.assertTrue((dst / "dotenv" / "package.json").is_file())

    def test_pnpm_store_flattens_to_sibling_layout(self):
        """The load-bearing test: sibling resolution must survive staging."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            top = build_pnpm_style_store(root)
            if count_symlinks(top) == 0:
                self.skipTest("environment could not create the pnpm symlink layout")

            dst = root / "staged"
            stats = merge_trees([top], dst)

            # No symlinks may remain, or the bundle breaks on Windows.
            self.assertEqual(count_symlinks(dst), 0, "symlinks survived staging")
            self.assertGreaterEqual(stats.symlinks_dereferenced, 1)

            # embedded-postgres and @embedded-postgres/windows-x64 must be siblings.
            wrapper = dst / WRAPPER_PACKAGE
            native = dst / NATIVE_SCOPE / "windows-x64"
            self.assertTrue(wrapper.is_dir(), "wrapper package was not staged")
            self.assertTrue(native.is_dir(), "native package was not staged")
            self.assertEqual(wrapper.parent, native.parent.parent)

            # And the app's own resolver must find it usable.
            status = inspect(dst, WIN)
            self.assertTrue(status.siblings_ok, status.notes)
            self.assertTrue(status.healthy, status.notes)
            self.assertTrue((native / "native" / "bin" / "postgres.exe").is_file())

    def test_missing_roots_are_ignored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            (real / "pkg").mkdir(parents=True)
            (real / "pkg" / "package.json").write_text("{}", encoding="utf-8")
            dst = root / "out"
            merge_trees([root / "absent", real], dst)
            self.assertTrue((dst / "pkg" / "package.json").is_file())


class EnsureExecutableBitsTests(unittest.TestCase):
    def test_restores_dropped_executable_bit(self):
        with TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "initdb.exe"
            binary.write_text("MZ\n", encoding="utf-8")
            binary.chmod(0o644)  # simulate a copy that dropped the bit

            self.assertFalse(binary.stat().st_mode & stat.S_IXUSR)
            fixed = ensure_executable_bits(bin_dir, ["initdb.exe", "postgres.exe"])

            self.assertEqual(fixed, ["initdb.exe"])
            self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)

    def test_already_executable_is_not_reported(self):
        with TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "postgres"
            binary.write_text("x\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(ensure_executable_bits(bin_dir, ["postgres"]), [])

    def test_missing_names_are_ignored(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(ensure_executable_bits(Path(tmp), ["nope"]), [])


if __name__ == "__main__":
    unittest.main()
