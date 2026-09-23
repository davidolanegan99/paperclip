"""Embedded PostgreSQL native-runtime introspection.

Why this module exists
----------------------
``embedded-postgres`` does **not** download Postgres at first run. Its binaries
ship as ``os``/``cpu``-gated npm ``optionalDependencies``
(``@embedded-postgres/windows-x64``, ``@embedded-postgres/linux-x64``, ...) that
the package manager installs at ``pnpm install`` time.

The app locates them *relative to the ``embedded-postgres`` package root* -- see
``packages/db/src/embedded-postgres-native.ts``:

    nativeRoot = path.resolve(packageRoot, "..", "@embedded-postgres", slug)
    libDir     = path.join(nativeRoot, "native", "lib")

That sibling-relative resolution is what makes packaging fragile:

* ``node_modules`` must contain ``embedded-postgres/`` and
  ``@embedded-postgres/<slug>/`` as **siblings in the same directory**.
* Under pnpm the real files live in the ``.pnpm`` virtual store behind
  symlinks. Windows cannot create symlinks without privilege and PyInstaller
  does not preserve them, so staging must dereference.
* On Linux the app creates ``libX.so.A`` aliases for ``libX.so.A.B`` at runtime
  (:func:`packages.db/src/embedded-postgres-native.ensureLinuxSharedLibraryAliases`),
  which needs a writable lib dir. We pre-create them at build time as real
  copies so runtime symlink creation is never required.

This module only *inspects*; staging lives in ``build_exe.py``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ._platform import ARM64, LINUX, MACOS, WINDOWS, X64, PlatformInfo, current_platform

#: npm scope holding the platform-specific Postgres binaries.
NATIVE_SCOPE = "@embedded-postgres"

#: The ``embedded-postgres`` wrapper package name.
WRAPPER_PACKAGE = "embedded-postgres"

#: Platform -> native package slug, mirroring the published optionalDependencies
#: (darwin-arm64, darwin-x64, linux-arm64, linux-arm, linux-ia32, linux-ppc64,
#: linux-x64, windows-x64).
NATIVE_SLUGS: Dict[Tuple[str, str], str] = {
    (WINDOWS, X64): "windows-x64",
    (LINUX, X64): "linux-x64",
    (LINUX, ARM64): "linux-arm64",
    (MACOS, X64): "darwin-x64",
    (MACOS, ARM64): "darwin-arm64",
}

#: Binaries that must exist for initdb/postgres to actually start.
REQUIRED_BINARIES: Tuple[str, ...] = ("initdb", "postgres", "pg_ctl")

#: Matches ``libX.so.A.B`` / ``libX.so.A.B.C`` -- same shape the app uses, so the
#: aliases we pre-create are exactly the ones it would have created.
_LINUX_LIB_ALIAS_RE = re.compile(r"^(lib.+\.so\.\d+)\.\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class EmbeddedPostgresStatus:
    """What we found (or failed to find) about the Postgres native runtime."""

    platform: PlatformInfo
    slug: Optional[str]
    package_dir: Optional[Path]
    present: bool
    wrapper_present: bool
    siblings_ok: bool
    missing_binaries: List[str] = field(default_factory=list)
    lib_dir: Optional[Path] = None
    aliases_created: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def supported_platform(self) -> bool:
        return self.slug is not None

    @property
    def healthy(self) -> bool:
        """True when an offline start of embedded Postgres should work."""
        return bool(
            self.supported_platform
            and self.present
            and self.siblings_ok
            and not self.missing_binaries
        )

    def describe(self) -> str:
        if not self.supported_platform:
            return f"unsupported platform {self.platform.os}/{self.platform.arch}"
        state = "healthy" if self.healthy else "INCOMPLETE"
        return f"@embedded-postgres/{self.slug} [{state}] at {self.package_dir}"


def native_slug(plat: Optional[PlatformInfo] = None) -> Optional[str]:
    """Native package slug for a platform, or ``None`` when unsupported.

    Mirrors ``resolveNativePackageName()`` in ``embedded-postgres-native.ts``,
    which returns null for anything outside the published set.
    """
    plat = plat or current_platform()
    return NATIVE_SLUGS.get((plat.os, plat.arch))


def wrapper_dir(node_modules: Path) -> Path:
    return node_modules / WRAPPER_PACKAGE


def native_package_dir(node_modules: Path, plat: Optional[PlatformInfo] = None) -> Optional[Path]:
    """Where the native package must sit to be a sibling of the wrapper."""
    slug = native_slug(plat)
    if slug is None:
        return None
    return node_modules / NATIVE_SCOPE / slug


def _find_binaries(bin_dir: Path, plat: PlatformInfo) -> List[str]:
    """Names from REQUIRED_BINARIES that are missing from ``bin_dir``."""
    missing: List[str] = []
    for name in REQUIRED_BINARIES:
        candidate = bin_dir / f"{name}{plat.exe_suffix}"
        if not candidate.is_file():
            missing.append(candidate.name)
    return missing


def inspect(node_modules: Optional[Path], plat: Optional[PlatformInfo] = None) -> EmbeddedPostgresStatus:
    """Inspect a staged or live ``node_modules`` for a usable Postgres runtime.

    Never raises: a missing ``node_modules`` simply yields an unhealthy status
    with explanatory notes, so ``--launcher-doctor`` can report rather than crash.
    """
    plat = plat or current_platform()
    slug = native_slug(plat)

    if slug is None:
        return EmbeddedPostgresStatus(
            platform=plat,
            slug=None,
            package_dir=None,
            present=False,
            wrapper_present=False,
            siblings_ok=False,
            notes=[
                f"no @embedded-postgres build is published for {plat.os}/{plat.arch};",
                "embedded Postgres cannot run here -- configure an external DATABASE_URL.",
            ],
        )

    if node_modules is None:
        return EmbeddedPostgresStatus(
            platform=plat,
            slug=slug,
            package_dir=None,
            present=False,
            wrapper_present=False,
            siblings_ok=False,
            notes=["no node_modules resolved, so the Postgres native package cannot be located"],
        )

    wrapper = wrapper_dir(node_modules)
    pkg_dir = native_package_dir(node_modules, plat)
    wrapper_present = wrapper.is_dir()
    present = bool(pkg_dir and pkg_dir.is_dir())
    # The app resolves the native package as a sibling of the wrapper, so both
    # must share the same node_modules root.
    siblings_ok = wrapper_present and present and wrapper.parent == (pkg_dir.parent.parent if pkg_dir else None)

    notes: List[str] = []
    missing: List[str] = []
    lib_dir: Optional[Path] = None

    if present and pkg_dir is not None:
        bin_dir = pkg_dir / "native" / "bin"
        lib_dir = pkg_dir / "native" / "lib"
        if not bin_dir.is_dir():
            notes.append(f"native/bin is missing under {pkg_dir}")
            missing = [f"{name}{plat.exe_suffix}" for name in REQUIRED_BINARIES]
        else:
            missing = _find_binaries(bin_dir, plat)
            if missing:
                notes.append(f"missing binaries: {', '.join(missing)}")
        if lib_dir is not None and not lib_dir.is_dir():
            lib_dir = None
    else:
        notes.append(
            f"expected {NATIVE_SCOPE}/{slug} inside {node_modules}; "
            "it is an optionalDependency, so it is absent when the install ran "
            "on a different OS/arch or with --no-optional."
        )

    if not wrapper_present:
        notes.append(f"{WRAPPER_PACKAGE} itself is missing from {node_modules}")

    if present and plat.os != LINUX and lib_dir is None:
        # Windows/macOS bundles keep DLLs/dylibs next to the binaries rather than
        # in native/lib, so this is informational rather than an error.
        notes.append("native/lib not present (normal on Windows/macOS)")

    return EmbeddedPostgresStatus(
        platform=plat,
        slug=slug,
        package_dir=pkg_dir,
        present=present,
        wrapper_present=wrapper_present,
        siblings_ok=siblings_ok,
        missing_binaries=missing,
        lib_dir=lib_dir,
        notes=notes,
    )


def pending_linux_lib_aliases(lib_dir: Path) -> List[Tuple[Path, Path]]:
    """``(source, alias)`` pairs the app would need to create at runtime.

    Mirrors ``ensureLinuxSharedLibraryAliases``: for ``libX.so.A.B[.C]`` the
    alias is ``libX.so.A``.
    """
    pairs: List[Tuple[Path, Path]] = []
    if not lib_dir.is_dir():
        return pairs
    for entry in sorted(lib_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _LINUX_LIB_ALIAS_RE.match(entry.name)
        if not match:
            continue
        alias = lib_dir / match.group(1)
        if alias.exists():
            continue
        pairs.append((entry, alias))
    return pairs


def precreate_linux_lib_aliases(lib_dir: Path, *, use_symlinks: bool = False) -> int:
    """Create the ``libX.so.A`` aliases at build time; returns how many.

    Defaults to **real copies** rather than symlinks: a PyInstaller archive does
    not reliably preserve symlinks (and Windows cannot create them without
    privilege), while the dynamic loader is equally happy with a copy. The app
    tolerates pre-existing aliases (it skips ``EEXIST``), so doing this at build
    time removes the need for a writable lib dir at runtime.
    """
    import shutil

    created = 0
    for source, alias in pending_linux_lib_aliases(lib_dir):
        try:
            if use_symlinks:
                os.symlink(source.name, alias)
            else:
                shutil.copy2(source, alias)
            created += 1
        except (OSError, shutil.Error):
            # Best effort: the app will try again at runtime if it can write.
            continue
    return created


def linux_lib_path_entries(status: EmbeddedPostgresStatus) -> List[str]:
    """Directories to prepend to ``LD_LIBRARY_PATH`` for the child process.

    Belt-and-braces: the app sets this itself via
    ``prepareEmbeddedPostgresNativeRuntime()``, but doing it in the launcher
    guarantees the loader can find libpq/libssl even for subprocesses the app
    spawns before that hook runs.
    """
    if status.platform.os != LINUX or status.lib_dir is None:
        return []
    return [str(status.lib_dir)]


def windows_path_entries(status: EmbeddedPostgresStatus) -> List[str]:
    """Directories to prepend to ``PATH`` on Windows for embedded Postgres.

    On Windows the Postgres binaries (initdb, postgres, pg_ctl) need their
    companion DLLs (libpq, libssl, etc.) which live in native/bin and
    native/lib. The app itself does not set PATH, so the launcher must do it
    to ensure offline Postgres starts even when the payload is in a read-only
    frozen bundle. Returns [bin_dir, lib_dir] when available.
    """
    if status.platform.os != WINDOWS or status.package_dir is None:
        return []
    entries: List[str] = []
    bin_dir = status.package_dir / "native" / "bin"
    if bin_dir.is_dir():
        entries.append(str(bin_dir))
    if status.lib_dir is not None and status.lib_dir.is_dir():
        entries.append(str(status.lib_dir))
    # Also include native root lib if present (some distributions keep DLLs there)
    native_root = status.package_dir / "native"
    if native_root.is_dir() and str(native_root) not in entries:
        # Only add if it contains DLLs
        try:
            if any(p.suffix.lower() == ".dll" for p in native_root.iterdir() if p.is_file()):
                entries.append(str(native_root))
        except OSError:
            pass
    return entries


def darwin_path_entries(status: EmbeddedPostgresStatus) -> List[str]:
    """Directories to prepend to DYLD_LIBRARY_PATH on macOS if needed."""
    if status.platform.os != MACOS or status.lib_dir is None:
        return []
    return [str(status.lib_dir)]


def doctor_lines(status: EmbeddedPostgresStatus) -> List[str]:
    """Human-readable doctor output for the Postgres native runtime."""
    lines: List[str] = []
    if not status.supported_platform:
        lines.append(f"  n/a no published build for {status.platform.os}/{status.platform.arch}")
        lines.extend(f"      {note}" for note in status.notes)
        return lines

    marker = "OK  " if status.healthy else "WARN"
    lines.append(f"  {marker} {status.describe()}")
    lines.append(f"      wrapper package : {'present' if status.wrapper_present else 'MISSING'}")
    lines.append(f"      sibling layout  : {'correct' if status.siblings_ok else 'BROKEN'}")
    if status.lib_dir is not None:
        lines.append(f"      native/lib      : {status.lib_dir}")
    if status.missing_binaries:
        lines.append(f"      missing binaries: {', '.join(status.missing_binaries)}")
    for note in status.notes:
        lines.append(f"      note: {note}")
    if not status.healthy:
        lines.append(
            "      embedded Postgres will not start offline. Either reinstall deps on this"
        )
        lines.append(
            "      OS/arch (pnpm install), or point Paperclip at an external DATABASE_URL."
        )
    return lines
