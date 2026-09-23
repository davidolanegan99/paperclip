"""Node.js runtime discovery, version handling and portable-runtime download.

Resolution order (first healthy candidate wins):

1. ``PAPERCLIP_NODE`` env var -- explicit override, always honoured.
2. Node bundled *inside* the executable (``runtime/node``), then next to it.
3. ``node`` on ``PATH``, subject to the minimum-version gate.

The portable runtime can be fetched at build time (preferred, embedded in the
exe) or lazily at first run into the user data dir when ``--launcher-fetch-node``
was used or ``PAPERCLIP_FETCH_NODE=1`` is set.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from . import MINIMUM_NODE_VERSION
from ._platform import (
    ARM64,
    LINUX,
    MACOS,
    WINDOWS,
    X64,
    PlatformInfo,
    bundled_root,
    current_platform,
    env_flag,
    env_path,
)

#: nodejs.org distribution mirror. Overridable for corporate mirrors/proxies.
NODE_DIST_MIRROR = os.environ.get("PAPERCLIP_NODE_MIRROR", "https://nodejs.org/dist").rstrip("/")

#: Node version embedded when the build requests a portable runtime.
#: Node 24 is the LTS line that satisfies MINIMUM_NODE_VERSION (24.11.0).
DEFAULT_NODE_VERSION = os.environ.get("PAPERCLIP_NODE_VERSION", "v24.11.0")

#: Where a lazily-downloaded runtime is cached.
RUNTIME_CACHE_DIRNAME = "runtime"

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class NodeRuntimeError(RuntimeError):
    """Raised when no usable Node.js runtime can be found or prepared."""


@dataclass(frozen=True)
class NodeVersion:
    """A parsed ``major.minor.patch`` triple with comparison semantics."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "NodeVersion":
        match = _VERSION_RE.search(text or "")
        if not match:
            raise NodeRuntimeError(f"could not parse Node version from {text!r}")
        major, minor, patch = (int(g) for g in match.groups())
        return cls(major, minor, patch)

    def as_tuple(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def satisfies_minimum(self, minimum: str = MINIMUM_NODE_VERSION) -> bool:
        return self.as_tuple() >= NodeVersion.parse(minimum).as_tuple()


@dataclass(frozen=True)
class NodeRuntime:
    """A resolved, ready-to-exec Node.js binary."""

    path: Path
    version: Optional[NodeVersion]
    source: str

    def describe(self) -> str:
        version = str(self.version) if self.version else "unknown"
        return f"node {version} [{self.source}] -> {self.path}"


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


def probe_node(path: Path, timeout: float = 20.0) -> Optional[NodeVersion]:
    """Return the version reported by ``path --version``, or ``None`` if unusable.

    Never raises: a missing, non-executable, or crashing candidate is simply
    rejected so resolution can continue to the next option.
    """
    try:
        if not path.exists():
            return None
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # Never let a probe open a console window on Windows.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return None
    try:
        return NodeVersion.parse(stdout)
    except NodeRuntimeError:
        return None


def _node_candidates_in(directory: Path, plat: PlatformInfo) -> List[Tuple[Path, str]]:
    """Node binaries that may live inside ``directory`` (bundled layouts)."""
    candidates: List[Tuple[Path, str]] = []
    if not directory.is_dir():
        return candidates
    rel = plat.node_bin_relpath
    for label, sub in (
        ("bundled", ""),
        ("bundled", "node"),
        ("bundled", RUNTIME_CACHE_DIRNAME),
        ("bundled", f"node/{RUNTIME_CACHE_DIRNAME}"),
    ):
        candidates.append((directory / sub / rel if sub else directory / rel, label))
    return [(p, label) for p, label in candidates if p.is_file()]


def bundled_node_candidates(plat: Optional[PlatformInfo] = None) -> List[Tuple[Path, str]]:
    """Every Node binary shipped inside (or beside) the executable."""
    plat = plat or current_platform()
    roots = [
        bundled_root() / "runtime",
        bundled_root(),
        bundled_root().parent / "runtime",
    ]
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        roots += [here / "runtime", here]

    seen: set = set()
    out: List[Tuple[Path, str]] = []
    for root in roots:
        try:
            key = root.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.extend(_node_candidates_in(root, plat))
    return out


def path_node_candidates(plat: Optional[PlatformInfo] = None) -> List[Tuple[Path, str]]:
    """``node`` resolved from ``PATH`` (plus ``node.exe`` beside us on Windows)."""
    plat = plat or current_platform()
    name = f"node{plat.exe_suffix}"
    found = shutil.which(name) or shutil.which("node")
    out: List[Tuple[Path, str]] = []
    if found:
        out.append((Path(found), "PATH"))
    return out


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def resolve_node(
    plat: Optional[PlatformInfo] = None,
    minimum: str = MINIMUM_NODE_VERSION,
    allow_fetch: bool = False,
    cache_dir: Optional[Path] = None,
) -> NodeRuntime:
    """Find a Node runtime that satisfies ``minimum``, or raise.

    Parameters
    ----------
    allow_fetch:
        When no local runtime qualifies, download a portable Node into
        ``cache_dir`` and use it. Off by default so a frozen exe never touches
        the network unexpectedly.
    """
    plat = plat or current_platform()
    minimum_version = NodeVersion.parse(minimum)
    rejections: List[str] = []

    def consider(path: Path, source: str, *, enforce_minimum: bool) -> Optional[NodeRuntime]:
        version = probe_node(path)
        if version is None:
            rejections.append(f"{source}: {path} did not report a version")
            return None
        if enforce_minimum and version.as_tuple() < minimum_version.as_tuple():
            rejections.append(f"{source}: {path} is {version} (need >= {minimum_version})")
            return None
        return NodeRuntime(path=path, version=version, source=source)

    # 1. Explicit override wins and is trusted verbatim (still version-checked
    #    so we can emit a precise diagnostic rather than a confusing crash).
    override = env_path("PAPERCLIP_NODE")
    if override is not None:
        runtime = consider(override, "PAPERCLIP_NODE", enforce_minimum=True)
        if runtime:
            return runtime

    # 2. Bundled inside the executable -- no minimum gate needed beyond the
    #    default, but we still record the version for diagnostics.
    for path, label in bundled_node_candidates(plat):
        runtime = consider(path, label, enforce_minimum=True)
        if runtime:
            return runtime

    # 3. Cache directory (lazily fetched runtime from a previous run).
    for directory in _cache_dirs(cache_dir):
        for path, label in _node_candidates_in(directory, plat):
            runtime = consider(path, f"cached {label}", enforce_minimum=True)
            if runtime:
                return runtime

    # 4. System PATH.
    for path, label in path_node_candidates(plat):
        runtime = consider(path, label, enforce_minimum=True)
        if runtime:
            return runtime

    # 5. Optionally fetch a portable runtime.
    if allow_fetch or env_flag("PAPERCLIP_FETCH_NODE"):
        target = _fetch_target_dir(cache_dir)
        target.mkdir(parents=True, exist_ok=True)
        path = install_portable_node(target, plat=plat)
        runtime = consider(path, "downloaded", enforce_minimum=True)
        if runtime:
            return runtime

    raise NodeRuntimeError(_no_node_message(minimum_version, rejections, allow_fetch))


def _cache_dirs(explicit: Optional[Path]) -> Iterable[Path]:
    if explicit is not None:
        yield explicit
    from .payload import data_dir  # local import: avoids a cycle at import time

    yield data_dir() / RUNTIME_CACHE_DIRNAME


def _fetch_target_dir(cache_dir: Optional[Path]) -> Path:
    if cache_dir is not None:
        return cache_dir
    from .payload import data_dir

    return data_dir() / RUNTIME_CACHE_DIRNAME


def _no_node_message(minimum: NodeVersion, rejections: Sequence[str], allow_fetch: bool) -> str:
    lines = [
        f"Paperclip needs Node.js >= {minimum} to run, and no suitable runtime was found.",
        "",
    ]
    if rejections:
        lines.append("Candidates considered:")
        lines.extend(f"  - {item}" for item in rejections)
        lines.append("")
    lines.append("Fix it in one of these ways:")
    lines.append(f"  1. Install Node.js {minimum} or newer from https://nodejs.org/ and re-run.")
    lines.append("  2. Point at an existing install:  set PAPERCLIP_NODE=C:\\path\\to\\node.exe")
    if not allow_fetch:
        lines.append("  3. Let Paperclip fetch one for you: set PAPERCLIP_FETCH_NODE=1")
    lines.append("  4. Rebuild the exe with an embedded runtime: build_exe.py --embed-node")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Portable runtime download / extraction
# --------------------------------------------------------------------------- #


def node_dist_url(version: str, plat: PlatformInfo) -> str:
    """Distribution archive URL for ``version`` on ``plat``."""
    if not version.startswith("v"):
        version = f"v{version}"
    stem = f"node-{version}-{_dist_platform_slug(plat)}"
    return f"{NODE_DIST_MIRROR}/{version}/{stem}{plat.node_archive_ext}"


def _dist_platform_slug(plat: PlatformInfo) -> str:
    os_slug = {WINDOWS: "win", MACOS: "darwin", LINUX: "linux"}[plat.os]
    arch_slug = {X64: "x64", ARM64: "arm64"}[plat.arch]
    if plat.os == WINDOWS:
        return f"{os_slug}-{arch_slug}"
    return f"{os_slug}-{arch_slug}"


def _strip_top_level(directory: Path) -> Optional[Path]:
    """If ``directory`` holds exactly one subdir (the archive's root), return it."""
    entries = [p for p in directory.iterdir() if p.name not in {"__MACOSX"}]
    subdirs = [p for p in entries if p.is_dir()]
    if len(entries) == 1 and len(subdirs) == 1:
        return subdirs[0]
    return None


#: ``tarfile``'s path-traversal-safe extraction filter landed in Python 3.12
#: (and 3.11.4+ as a backport). Fall back gracefully on older interpreters so
#: the builder keeps working on the 3.9+ floor this package supports.
_TARFILE_SUPPORTS_FILTER = hasattr(tarfile, "data_filter")


def _safe_extractall(tf: "tarfile.TarFile", destination: Path) -> None:
    """Extract a tarball, using the safe ``data`` filter when available."""
    if _TARFILE_SUPPORTS_FILTER:
        tf.extractall(destination, filter="data")
    else:  # pragma: no cover - exercised only on Python < 3.11.4
        tf.extractall(destination)


def _flatten_single_subdir(destination: Path) -> None:
    """Hoist a lone top-level directory's contents up into ``destination``.

    Node distributions always wrap everything in ``node-vX-<os>-<arch>/``.
    Flattening makes the installed layout deterministic
    (``destination/bin/node``, ``destination/node.exe``), which is what lets
    :func:`install_portable_node` detect an existing install and skip the
    download instead of re-fetching ~30 MB on every run.
    """
    nested = _strip_top_level(destination)
    if nested is None:
        return
    # Move via a sibling temp name so we never collide with the target.
    staging = destination.parent / f"{destination.name}.flatten-tmp"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    nested.rename(staging)
    shutil.rmtree(destination, ignore_errors=True)
    staging.rename(destination)


def extract_node_archive(archive: Path, destination: Path, plat: PlatformInfo) -> Path:
    """Extract a Node distribution archive, returning the directory holding ``node``.

    The result is always rooted at ``destination`` regardless of how the archive
    was nested, so callers can rely on ``destination / plat.node_bin_relpath``.

    Extraction is deterministic: any previous contents of ``destination`` are
    removed first. Without this, re-extracting merges the archive's top-level
    ``node-vX-.../`` directory back in alongside the flattened copy, which both
    wastes disk and defeats the "already installed" check.
    """
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            _safe_extractall(tf, destination)
    elif name.endswith(".tar.xz"):
        with tarfile.open(archive, "r:xz") as tf:
            _safe_extractall(tf, destination)
    else:
        raise NodeRuntimeError(f"unsupported Node archive format: {archive.name}")

    _flatten_single_subdir(destination)

    root = destination
    node_bin = root / plat.node_bin_relpath
    if not node_bin.is_file():
        # Fall back to a search: some mirrors repack slightly differently.
        matches = [m for m in sorted(root.rglob(f"node{plat.exe_suffix}")) if m.is_file()]
        if not matches:
            raise NodeRuntimeError(
                f"extracted {archive.name} but found no node binary at {node_bin}"
            )
        node_bin = matches[0]
        root = node_bin.parent
    if plat.os != WINDOWS:
        try:
            node_bin.chmod(0o755)
        except OSError:
            pass  # read-only filesystem; the binary is already executable upstream
    return root


def download_file(url: str, destination: Path, *, chunk_size: int = 1 << 20) -> Path:
    """Stream ``url`` to ``destination`` with a simple progress report on a TTY."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "paperclip-exe-builder/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 (https URL)
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            interactive = sys.stderr.isatty()
            with open(tmp, "wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if interactive and total:
                        pct = done * 100 // total
                        sys.stderr.write(f"\r  downloading {destination.name}: {pct:3d}%")
                        sys.stderr.flush()
            if interactive and total:
                sys.stderr.write("\n")
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise NodeRuntimeError(f"download failed for {url}: {exc}") from exc
    tmp.replace(destination)
    return destination


def install_portable_node(
    destination: Path,
    plat: Optional[PlatformInfo] = None,
    version: str = DEFAULT_NODE_VERSION,
    *,
    keep_archive: bool = False,
) -> Path:
    """Download and unpack a portable Node into ``destination``.

    Returns the path to the ``node`` binary. Skips the download when a usable
    runtime already exists at the destination.
    """
    plat = plat or current_platform()
    node_bin = destination / plat.node_bin_relpath
    if node_bin.is_file() and probe_node(node_bin) is not None:
        return node_bin

    url = node_dist_url(version, plat)
    archive = destination.parent / f"{destination.name}{plat.node_archive_ext}"
    print(f"Fetching portable Node {version} for {plat.os}/{plat.arch}...", file=sys.stderr)
    print(f"  {url}", file=sys.stderr)
    download_file(url, archive)
    root = extract_node_archive(archive, destination, plat)
    if not keep_archive:
        archive.unlink(missing_ok=True)
    resolved = root / plat.node_bin_relpath
    if not resolved.is_file():
        resolved = node_bin
    if not resolved.is_file():
        raise NodeRuntimeError(f"portable Node install failed: no binary under {destination}")
    return resolved
