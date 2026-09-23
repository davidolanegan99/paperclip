"""Locating the Paperclip application payload and the user data directory.

The payload is the *unmodified* upstream build output:

    payload/
      app/
        index.js          esbuild ESM bundle (cli/dist/index.js)
        package.json      declares the external npm deps the bundle imports
      node_modules/       those external deps (commander, embedded-postgres, ...)
      assets/             server/ui dist, skills, migrations, runner vendor

Keeping ``node_modules`` as a sibling of ``app/`` is what makes Node's resolver
find the externals, so the bundle runs exactly as it does from npm.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ._platform import app_root, bundled_root, current_platform, env_flag, env_path

#: Env var that pins the payload root explicitly (highest priority).
PAYLOAD_ENV = "PAPERCLIP_PAYLOAD"
#: Env var that pins the entry JS file explicitly.
ENTRY_ENV = "PAPERCLIP_ENTRY"
#: Env var that overrides the user data directory.
DATA_DIR_ENV = "PAPERCLIP_DATA_DIR"
#: Env var that disables discovery fallbacks, so only explicit config is used.
STRICT_ENV = "PAPERCLIP_STRICT_PAYLOAD"

#: Candidate payload roots relative to a search anchor, in priority order.
_PAYLOAD_RELPATHS = (
    Path("payload"),
    Path("_internal") / "payload",
    Path("."),
)

#: Candidate entry files relative to a payload's ``app`` dir (or the root).
_ENTRY_RELPATHS = (
    Path("app") / "index.js",
    Path("app") / "index.mjs",
    Path("index.js"),
    Path("cli") / "dist" / "index.js",
    Path("dist") / "index.js",
)


class PayloadError(RuntimeError):
    """Raised when the application payload cannot be located."""


@dataclass(frozen=True)
class Payload:
    """A resolved application payload ready to be handed to Node."""

    root: Path
    entry: Path
    node_modules: Optional[Path]
    assets: List[Path] = field(default_factory=list)
    source: str = "discovered"

    @property
    def cwd(self) -> Path:
        """Working directory for the child process (entry's parent)."""
        return self.entry.parent

    def describe(self) -> str:
        modules = str(self.node_modules) if self.node_modules else "none"
        return f"payload [{self.source}] entry={self.entry} node_modules={modules}"


def data_dir() -> Path:
    """Per-user directory for cached runtimes, logs and extracted assets.

    Follows platform convention: ``%LOCALAPPDATA%\\paperclip`` on Windows,
    ``~/Library/Application Support/paperclip`` on macOS, ``~/.paperclip``
    elsewhere. Honours ``PAPERCLIP_DATA_DIR``.
    """
    override = env_path(DATA_DIR_ENV)
    if override is not None:
        return override
    plat = current_platform()
    if plat.os == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "paperclip"
    if plat.os == "macos":
        return Path.home() / "Library" / "Application Support" / "paperclip"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "paperclip"


def is_strict() -> bool:
    """Whether discovery fallbacks are disabled (explicit config only)."""
    return env_flag(STRICT_ENV)


def _configured_roots() -> List[Path]:
    """Payload roots named explicitly via the environment.

    ``PAPERCLIP_PAYLOAD`` is treated as the payload root *itself*, not as a
    directory to search beneath -- otherwise a sibling ``payload/`` folder could
    silently win over an explicit configuration.
    """
    roots: List[Path] = []
    override = env_path(PAYLOAD_ENV)
    if override is not None:
        roots.append(override)
    return roots


def _search_anchors() -> List[Path]:
    """Directories that may contain a ``payload`` folder, in priority order.

    When frozen, the search is deliberately narrow: the bundled payload, then
    one beside the executable, then the cwd (so a user can drop an updated
    payload next to ``paperclip.exe``). It never walks back to the source tree,
    because a shipped exe must not silently pick up a developer checkout.

    Set ``PAPERCLIP_STRICT_PAYLOAD=1`` to disable every fallback and require an
    explicit ``PAPERCLIP_PAYLOAD``/``PAPERCLIP_ENTRY``.
    """
    if is_strict():
        return []

    anchors: List[Path] = []
    # An explicit root is also a reasonable place to look for a nested payload.
    override = env_path(PAYLOAD_ENV)
    if override is not None:
        anchors.append(override.parent)

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            anchors.append(Path(meipass))
        anchors.append(Path(sys.executable).resolve().parent)
        anchors.append(Path.cwd())
    else:
        # Running from source: the packaging dir, the cwd, and the repo root are
        # all legitimate places to find a staged or in-tree payload.
        anchors.append(bundled_root())
        anchors.append(app_root())
        anchors.append(Path.cwd())

    return _dedupe(anchors)


def _dedupe(anchors: List[Path]) -> List[Path]:
    """Deduplicate while preserving order and resolving symlinks safely."""
    seen = set()
    unique: List[Path] = []
    for anchor in anchors:
        try:
            key = anchor.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(anchor)
    return unique


def _find_entry(candidate: Path) -> Optional[Path]:
    """Return the entry JS inside ``candidate`` if one of the known paths exists."""
    for rel in _ENTRY_RELPATHS:
        path = candidate / rel
        if path.is_file():
            return path
    return None


def _find_node_modules(payload_root: Path, entry: Path) -> Optional[Path]:
    """Nearest ``node_modules`` that Node's resolver will consult for externals."""
    candidates = [
        payload_root / "node_modules",
        payload_root / "app" / "node_modules",
        entry.parent / "node_modules",
        entry.parent.parent / "node_modules",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _collect_assets(payload_root: Path) -> List[Path]:
    """Bundled asset directories worth exposing via NODE_PATH-style discovery."""
    out: List[Path] = []
    for name in ("assets", "server", "ui", "skills", "vendor"):
        candidate = payload_root / name
        if candidate.is_dir():
            out.append(candidate)
    return out


def find_payload() -> Payload:
    """Locate the application payload, or raise :class:`PayloadError`.

    Priority: ``PAPERCLIP_ENTRY`` -> ``PAPERCLIP_PAYLOAD`` -> frozen bundle ->
    beside-the-executable -> current working directory.
    """
    # 1. Explicit entry file override.
    entry_override = env_path(ENTRY_ENV)
    if entry_override is not None:
        if not entry_override.is_file():
            raise PayloadError(f"{ENTRY_ENV} points at a missing file: {entry_override}")
        root = entry_override.parent.parent
        return Payload(
            root=root,
            entry=entry_override,
            node_modules=_find_node_modules(root, entry_override),
            assets=_collect_assets(root),
            source=ENTRY_ENV,
        )

    searched: List[str] = []

    # 2. Explicitly configured roots, treated as the payload root itself.
    for root in _configured_roots():
        searched.append(str(root))
        entry = _find_entry(root)
        if entry is not None:
            return Payload(
                root=root,
                entry=entry,
                node_modules=_find_node_modules(root, entry),
                assets=_collect_assets(root),
                source=PAYLOAD_ENV,
            )
        if is_strict():
            raise PayloadError(_strict_payload_message(root))

    # 3. Conventional discovery under each search anchor.
    for anchor in _search_anchors():
        for rel in _PAYLOAD_RELPATHS:
            candidate = anchor.resolve() if str(rel) == "." else (anchor / rel).resolve()
            searched.append(str(candidate))
            entry = _find_entry(candidate)
            if entry is None:
                continue
            return Payload(
                root=candidate,
                entry=entry,
                node_modules=_find_node_modules(candidate, entry),
                assets=_collect_assets(candidate),
                source=f"{anchor.name}/{rel}",
            )

    raise PayloadError(_no_payload_message(searched))


def _strict_payload_message(root: Path) -> str:
    return "\n".join(
        [
            f"{PAYLOAD_ENV} points at a directory with no recognisable entry point: {root}",
            "",
            f"Strict mode ({STRICT_ENV}=1) disables discovery fallbacks, so nothing else was tried.",
            f"Expected one of: {', '.join(str(rel) for rel in _ENTRY_RELPATHS)}",
            "",
            f"Fix it by pointing {PAYLOAD_ENV} at the payload root (the directory containing app/index.js),",
            f"or unset {STRICT_ENV} to allow discovery.",
        ]
    )


def _no_payload_message(searched: List[str]) -> str:
    lines = [
        "Could not find the Paperclip application payload.",
        "",
        "Searched (in order):",
    ]
    lines.extend(f"  - {item}" for item in searched[:12])
    if len(searched) > 12:
        lines.append(f"  ... and {len(searched) - 12} more")
    lines += [
        "",
        "The payload must contain app/index.js (the esbuild CLI bundle).",
        "Fix it in one of these ways:",
        f"  1. Rebuild with the payload staged: build_exe.py --stage-payload",
        f"  2. Point at an existing build:  set {PAYLOAD_ENV}=C:\\path\\to\\payload",
        f"  3. Point straight at the entry: set {ENTRY_ENV}=C:\\path\\to\\cli\\dist\\index.js",
    ]
    return "\n".join(lines)


def payload_manifest(payload: Payload) -> dict:
    """Serialisable description of a payload, used by ``--launcher-info``."""
    version = None
    pkg = payload.root / "app" / "package.json"
    if not pkg.is_file():
        pkg = payload.root / "package.json"
    if pkg.is_file():
        try:
            version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
        except (OSError, json.JSONDecodeError):
            version = None
    return {
        "root": str(payload.root),
        "entry": str(payload.entry),
        "node_modules": str(payload.node_modules) if payload.node_modules else None,
        "assets": [str(item) for item in payload.assets],
        "source": payload.source,
        "app_version": version,
    }
