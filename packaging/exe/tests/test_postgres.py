"""Tests for embedded-postgres native runtime introspection.

The behaviour under test mirrors ``packages/db/src/embedded-postgres-native.ts``,
which resolves the native package as a *sibling* of ``embedded-postgres``:

    nativeRoot = path.resolve(packageRoot, "..", "@embedded-postgres", slug)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe._platform import (  # noqa: E402
    ARM64,
    LINUX,
    MACOS,
    WINDOWS,
    X64,
    platform_from_keys,
)
from paperclip_exe.postgres import (  # noqa: E402
    NATIVE_SCOPE,
    REQUIRED_BINARIES,
    WRAPPER_PACKAGE,
    doctor_lines,
    inspect,
    linux_lib_path_entries,
    native_package_dir,
    native_slug,
    pending_linux_lib_aliases,
    precreate_linux_lib_aliases,
)

WIN = platform_from_keys(WINDOWS, X64)
LINUX_X64 = platform_from_keys(LINUX, X64)
LINUX_ARM64 = platform_from_keys(LINUX, ARM64)
MAC_ARM64 = platform_from_keys(MACOS, ARM64)


def make_native_package(
    node_modules: Path,
    slug: str,
    *,
    plat,
    binaries=REQUIRED_BINARIES,
    libs=(),
    wrapper: bool = True,
) -> Path:
    """Create a realistic ``@embedded-postgres/<slug>`` tree."""
    pkg = node_modules / NATIVE_SCOPE / slug
    bin_dir = pkg / "native" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in binaries:
        path = bin_dir / f"{name}{plat.exe_suffix}"
        path.write_text("binary\n", encoding="utf-8")
        path.chmod(0o755)
    if libs:
        lib_dir = pkg / "native" / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        for name in libs:
            (lib_dir / name).write_text("lib\n", encoding="utf-8")
    if wrapper:
        wrapper_dir = node_modules / WRAPPER_PACKAGE / "dist"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        (wrapper_dir / "index.js").write_text("//\n", encoding="utf-8")
        (node_modules / WRAPPER_PACKAGE / "package.json").write_text(
            '{"name":"embedded-postgres"}', encoding="utf-8"
        )
    return pkg


class NativeSlugTests(unittest.TestCase):
    def test_slug_matches_published_optional_dependencies(self):
        self.assertEqual(native_slug(WIN), "windows-x64")
        self.assertEqual(native_slug(LINUX_X64), "linux-x64")
        self.assertEqual(native_slug(LINUX_ARM64), "linux-arm64")
        self.assertEqual(native_slug(MAC_ARM64), "darwin-arm64")
        self.assertEqual(native_slug(platform_from_keys(MACOS, X64)), "darwin-x64")

    def test_unsupported_architecture_has_no_slug(self):
        # windows/arm64 is not published, so embedded Postgres cannot work there.
        self.assertIsNone(native_slug(platform_from_keys(WINDOWS, ARM64)))

    def test_native_package_dir_is_sibling_of_wrapper(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            expected = modules / NATIVE_SCOPE / "windows-x64"
            self.assertEqual(native_package_dir(modules, WIN), expected)
            # The app resolves `..` from the wrapper package root, so both must
            # share the same node_modules parent.
            wrapper_root = modules / WRAPPER_PACKAGE
            self.assertEqual(wrapper_root.parent, expected.parent.parent)


class InspectTests(unittest.TestCase):
    def test_healthy_windows_install(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            pkg = make_native_package(modules, "windows-x64", plat=WIN)
            status = inspect(modules, WIN)
            self.assertTrue(status.healthy, status.notes)
            self.assertTrue(status.present)
            self.assertTrue(status.wrapper_present)
            self.assertTrue(status.siblings_ok)
            self.assertEqual(status.missing_binaries, [])
            self.assertEqual(status.package_dir, pkg)

    def test_missing_native_package_is_unhealthy(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            make_native_package(modules, "linux-x64", plat=LINUX_X64)
            # Ask about Windows: only the linux package is staged (the usual
            # result of installing deps on the wrong platform).
            status = inspect(modules, WIN)
            self.assertFalse(status.healthy)
            self.assertFalse(status.present)
            self.assertTrue(any("optionalDependency" in note for note in status.notes))

    def test_missing_binary_is_reported(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            make_native_package(modules, "windows-x64", plat=WIN, binaries=("initdb", "postgres"))
            status = inspect(modules, WIN)
            self.assertFalse(status.healthy)
            self.assertIn("pg_ctl.exe", status.missing_binaries)

    def test_missing_wrapper_package_is_reported(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            make_native_package(modules, "windows-x64", plat=WIN, wrapper=False)
            status = inspect(modules, WIN)
            self.assertFalse(status.wrapper_present)
            self.assertFalse(status.siblings_ok)
            self.assertTrue(any(WRAPPER_PACKAGE in note for note in status.notes))

    def test_none_node_modules_does_not_raise(self):
        status = inspect(None, WIN)
        self.assertFalse(status.healthy)
        self.assertTrue(status.notes)

    def test_unsupported_platform_is_reported_not_an_error(self):
        status = inspect(Path("/whatever"), platform_from_keys(WINDOWS, ARM64))
        self.assertFalse(status.supported_platform)
        self.assertFalse(status.healthy)
        self.assertIn("unsupported platform", status.describe())

    def test_native_bin_directory_absent(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            pkg = modules / NATIVE_SCOPE / "windows-x64"
            pkg.mkdir(parents=True)
            (modules / WRAPPER_PACKAGE).mkdir(parents=True)
            status = inspect(modules, WIN)
            self.assertFalse(status.healthy)
            self.assertTrue(any("native/bin is missing" in note for note in status.notes))


class LinuxLibAliasTests(unittest.TestCase):
    """The app creates libX.so.A aliases at runtime; we pre-create them."""

    def _lib_dir(self, tmp: Path, names) -> Path:
        modules = Path(tmp) / "node_modules"
        pkg = make_native_package(modules, "linux-x64", plat=LINUX_X64, libs=names)
        return pkg / "native" / "lib"

    def test_identifies_aliases_matching_the_app_regex(self):
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libpq.so.5.14", "libssl.so.1.1", "libz.so"])
            pending = pending_linux_lib_aliases(lib_dir)
            aliases = {alias.name for _source, alias in pending}
            # libX.so.A.B -> libX.so.A ; plain libz.so is not matched
            self.assertEqual(aliases, {"libpq.so.5", "libssl.so.1"})

    def test_three_component_numeric_versions_alias_to_first(self):
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libcrypto.so.1.0.2"])
            pending = pending_linux_lib_aliases(lib_dir)
            self.assertEqual([alias.name for _s, alias in pending], ["libcrypto.so.1"])

    def test_non_numeric_suffix_is_deliberately_not_aliased(self):
        # Mirrors the app's regex exactly: it anchors on `$` after digits, so the
        # OpenSSL-1.0 style `libcrypto.so.1.0.2k` is NOT aliased. Diverging here
        # would create aliases the app never expects.
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libcrypto.so.1.0.2k"])
            self.assertEqual(pending_linux_lib_aliases(lib_dir), [])
            self.assertEqual(precreate_linux_lib_aliases(lib_dir), 0)

    def test_existing_alias_is_left_alone(self):
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libpq.so.5.14", "libpq.so.5"])
            self.assertEqual(pending_linux_lib_aliases(lib_dir), [])

    def test_precreate_makes_real_files_not_symlinks(self):
        # Symlinks do not survive PyInstaller archives, so copies are required.
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libpq.so.5.14"])
            created = precreate_linux_lib_aliases(lib_dir)
            self.assertEqual(created, 1)
            alias = lib_dir / "libpq.so.5"
            self.assertTrue(alias.is_file())
            self.assertFalse(alias.is_symlink())

    def test_precreate_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            lib_dir = self._lib_dir(Path(tmp), ["libpq.so.5.14"])
            self.assertEqual(precreate_linux_lib_aliases(lib_dir), 1)
            # Second run finds nothing pending, exactly like the app's EEXIST skip.
            self.assertEqual(precreate_linux_lib_aliases(lib_dir), 0)
            self.assertEqual(pending_linux_lib_aliases(lib_dir), [])

    def test_precreate_on_missing_dir_is_a_noop(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(precreate_linux_lib_aliases(Path(tmp) / "nope"), 0)

    def test_ld_library_path_only_on_linux(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            pkg = make_native_package(modules, "linux-x64", plat=LINUX_X64, libs=["libpq.so.5.14"])
            status = inspect(modules, LINUX_X64)
            entries = linux_lib_path_entries(status)
            self.assertEqual(entries, [str(pkg / "native" / "lib")])

            win_status = inspect(modules, WIN)
            self.assertEqual(linux_lib_path_entries(win_status), [])


class DoctorLinesTests(unittest.TestCase):
    def test_healthy_report(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            make_native_package(modules, "windows-x64", plat=WIN)
            lines = doctor_lines(inspect(modules, WIN))
            joined = "\n".join(lines)
            self.assertIn("OK", joined)
            self.assertIn("windows-x64", joined)
            self.assertIn("sibling layout  : correct", joined)

    def test_unhealthy_report_tells_the_user_what_to_do(self):
        with TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            lines = doctor_lines(inspect(modules, WIN))
            joined = "\n".join(lines)
            self.assertIn("WARN", joined)
            self.assertIn("DATABASE_URL", joined)
            self.assertIn("pnpm install", joined)

    def test_unsupported_platform_report(self):
        lines = doctor_lines(inspect(Path("/x"), platform_from_keys(WINDOWS, ARM64)))
        self.assertIn("n/a", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
