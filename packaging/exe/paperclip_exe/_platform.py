"""Platform helpers shared by the launcher and the build orchestrator.

Everything here is dependency-free (stdlib only) so the launcher can be frozen
with PyInstaller without pulling in third-party packages.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Canonical platform keys used throughout the packaging pipeline.
WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"

#: Canonical architecture keys.
X64 = "x64"
ARM64 = "arm64"

#: Executable suffix per platform.
EXE_SUFFIX = {WINDOWS: ".exe", MACOS: "", LINUX: ""}

#: Node.js distribution archive extension per platform.
NODE_ARCHIVE_EXT = {WINDOWS: ".zip", MACOS: ".tar.gz", LINUX: ".tar.xz"}

#: Relative path from the Node distribution root to the node binary.
NODE_BIN_RELPATH = {
    WINDOWS: "node.exe",
    MACOS: "bin/node",
    LINUX: "bin/node",
}


@dataclass(frozen=True)
class PlatformInfo:
    """Normalised description of the machine we are running/building on."""

    os: str
    arch: str
    exe_suffix: str
    node_archive_ext: str
    node_bin_relpath: str
    is_frozen: bool

    @property
    def node_binary_name(self) -> str:
        return f"node{self.exe_suffix}"

    def describe(self) -> str:
        frozen = "frozen" if self.is_frozen else "source"
        return f"{self.os}/{self.arch} ({frozen})"


def detect_os() -> str:
    system = platform.system().lower()
    if system.startswith("win") or os.name == "nt":
        return WINDOWS
    if system == "darwin":
        return MACOS
    return LINUX


def detect_arch() -> str:
    """Normalise the machine architecture to ``x64`` or ``arm64``.

    Windows-on-ARM64 reports ``AMD64``/``x86_64`` under emulation, so the
    processor architecture environment variable is the authority there.
    """
    if detect_os() == WINDOWS:
        # PROCESSOR_ARCHITEW6432 is set when running under WOW64 emulation.
        reported = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.machine()
        ).lower()
        if reported in {"arm64", "aarch64"}:
            return ARM64
        return X64

    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return ARM64
    return X64


def current_platform() -> PlatformInfo:
    """Describe the platform we are executing on right now."""
    os_key = detect_os()
    return PlatformInfo(
        os=os_key,
        arch=detect_arch(),
        exe_suffix=EXE_SUFFIX[os_key],
        node_archive_ext=NODE_ARCHIVE_EXT[os_key],
        node_bin_relpath=NODE_BIN_RELPATH[os_key],
        is_frozen=bool(getattr(sys, "frozen", False)),
    )


def platform_from_keys(os_key: str, arch: str) -> PlatformInfo:
    """Describe an arbitrary target platform (used for cross-preparing payloads)."""
    if os_key not in EXE_SUFFIX:
        raise ValueError(f"unsupported target os: {os_key!r} (expected one of {sorted(EXE_SUFFIX)})")
    if arch not in {X64, ARM64}:
        raise ValueError(f"unsupported target arch: {arch!r} (expected 'x64' or 'arm64')")
    return PlatformInfo(
        os=os_key,
        arch=arch,
        exe_suffix=EXE_SUFFIX[os_key],
        node_archive_ext=NODE_ARCHIVE_EXT[os_key],
        node_bin_relpath=NODE_BIN_RELPATH[os_key],
        # A cross-prepared payload is never itself frozen.
        is_frozen=False,
    )


def bundled_root() -> Path:
    """Root directory of data shipped *inside* the frozen executable.

    PyInstaller onefile extracts bundled data to ``sys._MEIPASS``; in onedir mode
    (and when running from source) the package directory is the right anchor.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Running from source: packaging/exe/paperclip_exe/_platform.py -> packaging/exe
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    """Directory containing the launcher executable (or the repo when unfrozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_path(name: str) -> Optional[Path]:
    """Read a filesystem path from the environment, or ``None`` when unset/blank."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()
