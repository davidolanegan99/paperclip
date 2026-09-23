"""Copying dependency trees into a bundle that must survive freezing.

The staging problem is specific and easy to get wrong:

* pnpm's ``node_modules`` is a forest of **symlinks** into a ``.pnpm`` virtual
  store. Windows cannot create symlinks without privilege or developer mode, and
  PyInstaller archives do not reliably preserve them. Copying symlinks verbatim
  therefore produces a bundle that works on the build machine and breaks on the
  target.
* ``embedded-postgres`` resolves its native binaries as a **sibling** of its own
  package directory, so the flat layout must keep
  ``node_modules/embedded-postgres`` and ``node_modules/@embedded-postgres/<slug>``
  in the same parent.

:func:`copy_tree_materialized` therefore dereferences every symlink into real
files, with loop protection so pnpm's occasionally cyclic store cannot hang the
build.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

#: Directory names that never need to ship inside a bundle.
SKIP_DIR_NAMES: Set[str] = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".turbo",
    ".DS_Store",
}

#: File suffixes that are pure build noise.
SKIP_SUFFIXES: Set[str] = {".pyc", ".pyo", ".log", ".tmp", ".tsbuildinfo"}

#: Guard against pathological trees.
#: Increased from 200k to 1M because pnpm's virtual store can contain many
#: small files (e.g. discord-api-types payloads) and the full workspace has
#: 1300+ packages. The guard is still useful to catch infinite loops.
MAX_FILES = 1_000_000

#: Directories that should never be copied as top-level entries when merging
#: node_modules roots. The pnpm virtual store lives in node_modules/.pnpm and
#: is huge; we only need the symlinked packages at the top level, not the
#: store itself. Copying .pnpm would also duplicate every package.
SKIP_TOP_LEVEL_DIRS = {".pnpm", ".modules.yaml", ".pnpm-debug.log"}


class StagingError(RuntimeError):
    """Raised when a dependency tree cannot be staged."""


@dataclass
class CopyStats:
    """What a materialization pass did."""

    files: int = 0
    dirs: int = 0
    bytes: int = 0
    symlinks_dereferenced: int = 0
    broken_symlinks_skipped: int = 0
    loops_skipped: int = 0
    skipped_by_rule: int = 0

    def describe(self) -> str:
        return (
            f"{self.files} files, {self.dirs} dirs, "
            f"{self.bytes / (1 << 20):.1f} MiB, "
            f"{self.symlinks_dereferenced} symlinks dereferenced, "
            f"{self.broken_symlinks_skipped} broken links skipped"
        )


def _should_skip_dir(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def _should_skip_file(path: Path) -> bool:
    return path.suffix in SKIP_SUFFIXES


def _is_executable(mode: int) -> bool:
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def copy_tree_materialized(
    src: Path,
    dst: Path,
    *,
    stats: Optional[CopyStats] = None,
    _ancestors: Optional[Set[Path]] = None,
    extra_skip_dirs: Iterable[str] = (),
) -> CopyStats:
    """Recursively copy ``src`` to ``dst``, replacing symlinks with real files.

    Two properties are essential and easy to get wrong:

    * **Destination names come from the link, not its target.** pnpm links
      ``node_modules/foo`` at ``.pnpm/foo@1.2.3/node_modules/foo``; using the
      resolved basename would silently rename entries whenever the two differ.
    * **Loop detection is branch-scoped, not global.** A real directory may be
      reachable from many symlinks in different subtrees -- that is exactly how
      pnpm's store works. Only a directory that is its *own ancestor* is a loop.
      Treating shared directories as loops drops packages from the bundle.

    Broken links are skipped rather than failing the build, and the executable
    bit is preserved (Postgres binaries and ``.bin`` shims need it).
    """
    stats = stats or CopyStats()
    # A fresh set per branch: `ancestors | {real}` never mutates the caller's,
    # so sibling subtrees cannot poison each other.
    ancestors: Set[Path] = set() if _ancestors is None else _ancestors
    skip_dirs = set(extra_skip_dirs)

    if not src.exists():
        raise StagingError(f"source does not exist: {src}")

    # A symlinked root (pnpm links whole packages): resolve before descending.
    if src.is_symlink():
        try:
            src = src.resolve(strict=True)
        except OSError:
            stats.broken_symlinks_skipped += 1
            return stats
        stats.symlinks_dereferenced += 1

    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(src, dst, stats)
        return stats

    real = _safe_realpath(src)
    if real in ancestors:
        stats.loops_skipped += 1
        return stats

    dst.mkdir(parents=True, exist_ok=True)
    stats.dirs += 1
    ancestors = ancestors | {real}

    if stats.files > MAX_FILES:
        raise StagingError(f"refusing to stage more than {MAX_FILES} files from {src}")

    for entry in sorted(src.iterdir()):
        # The name we must use at the destination is the entry's own name.
        link_name = entry.name

        if entry.is_symlink():
            try:
                target = entry.resolve(strict=True)
            except OSError:
                # Dangling link (common for optional deps of other platforms).
                stats.broken_symlinks_skipped += 1
                continue
            stats.symlinks_dereferenced += 1
            entry = target

        if skip_dirs and any(part in skip_dirs for part in entry.parts):
            stats.skipped_by_rule += 1
            continue

        if entry.is_dir():
            if _should_skip_dir(Path(link_name)):
                stats.skipped_by_rule += 1
                continue
            copy_tree_materialized(
                entry,
                dst / link_name,
                stats=stats,
                _ancestors=ancestors,
                extra_skip_dirs=skip_dirs,
            )
        elif entry.is_file():
            if _should_skip_file(Path(link_name)):
                stats.skipped_by_rule += 1
                continue
            _copy_file(entry, dst / link_name, stats)

    return stats


def _copy_file(src: Path, dst: Path, stats: CopyStats) -> None:
    """Copy one file, preserving the executable bit."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst, follow_symlinks=True)
    except OSError:
        # Fall back to a plain byte copy when metadata cannot be preserved
        # (e.g. crossing filesystems with odd permission models).
        with open(src, "rb") as reader, open(dst, "wb") as writer:
            shutil.copyfileobj(reader, writer)
    try:
        mode = src.stat().st_mode
        if _is_executable(mode):
            current = dst.stat().st_mode
            dst.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    stats.files += 1
    try:
        stats.bytes += dst.stat().st_size
    except OSError:
        pass


def _safe_realpath(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def merge_trees(sources: Sequence[Path], dst: Path) -> CopyStats:
    """Materialize several ``node_modules`` roots into one flat tree.

    Later roots fill gaps without overwriting earlier ones, so a workspace-local
    ``node_modules`` wins over the hoisted root (matching Node's own resolution
    preference). The flat result is what preserves the
    ``embedded-postgres``/``@embedded-postgres/<slug>`` sibling relationship.

    The pnpm virtual store (node_modules/.pnpm) is skipped at the top level
    because it contains the entire dependency graph duplicated and would
    explode the file count. The symlinked packages at the top level are what
    matter; they are dereferenced into real files by copy_tree_materialized.
    """
    stats = CopyStats()
    for index, source in enumerate(sources):
        if not source.is_dir():
            continue
        if index == 0 and not dst.exists():
            # For the first source, we still need to avoid copying .pnpm as a
            # top-level dir, so we merge entry-by-entry even for the first root
            # if it contains a .pnpm folder (which the repo root does).
            if (source / ".pnpm").is_dir():
                # Manual merge to skip .pnpm
                for entry in sorted(source.iterdir()):
                    if entry.name in SKIP_TOP_LEVEL_DIRS:
                        stats.skipped_by_rule += 1
                        continue
                    target = dst / entry.name
                    if target.exists():
                        continue
                    if entry.is_symlink():
                        try:
                            resolved = entry.resolve(strict=True)
                        except OSError:
                            stats.broken_symlinks_skipped += 1
                            continue
                        stats.symlinks_dereferenced += 1
                        entry = resolved
                    if entry.is_dir():
                        copy_tree_materialized(entry, target, stats=stats)
                    elif entry.is_file():
                        _copy_file(entry, target, stats)
                continue
            stats = copy_tree_materialized(source, dst)
            continue
        # Merge entry-by-entry so existing packages are not clobbered.
        for entry in sorted(source.iterdir()):
            if entry.name in SKIP_TOP_LEVEL_DIRS:
                stats.skipped_by_rule += 1
                continue
            target = dst / entry.name
            if target.exists():
                continue
            if entry.is_symlink():
                try:
                    entry = entry.resolve(strict=True)
                except OSError:
                    stats.broken_symlinks_skipped += 1
                    continue
                stats.symlinks_dereferenced += 1
            if entry.is_dir():
                copy_tree_materialized(entry, target, stats=stats)
            elif entry.is_file():
                _copy_file(entry, target, stats)
    return stats


def ensure_executable_bits(directory: Path, names: Sequence[str]) -> List[str]:
    """Make sure the given binaries under ``directory`` are executable.

    Returns the names that were fixed. Extraction and copying can drop the
    executable bit, which turns ``initdb`` into a confusing EACCES at runtime.
    """
    fixed: List[str] = []
    for name in names:
        for path in directory.rglob(name):
            if not path.is_file():
                continue
            mode = path.stat().st_mode
            if _is_executable(mode):
                continue
            try:
                path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                fixed.append(name)
            except OSError:
                continue
    return fixed


def count_symlinks(root: Path) -> int:
    """How many symlinks remain under ``root`` -- should be 0 after staging."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            if os.path.islink(os.path.join(dirpath, name)):
                total += 1
    return total
