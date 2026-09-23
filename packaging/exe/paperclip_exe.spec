# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Paperclip executable.

Driven by ``build_exe.py`` -- run that rather than invoking PyInstaller directly
so the payload/Postgres/runtime stages happen first:

    python3 packaging/exe/build_exe.py --stage-payload --freeze

Two layouts are supported, selected by ``PAPERCLIP_ONEDIR``:

* **onefile** (default) -- one self-extracting ``dist/paperclip.exe``. Most
  convenient, but it unpacks into ``%TEMP%`` at each start, which is the pattern
  AV heuristics flag most often.
* **onedir** -- ``dist/paperclip/paperclip.exe`` plus a ``_internal`` folder.
  Starts faster and produces far fewer false positives. Zip the folder to ship.

Layout frozen into the binary
-----------------------------
    _MEIPASS/ (onefile)  or  dist/paperclip/_internal/ (onedir)
      paperclip_exe/          the launcher package
      runtime/node/           portable Node.js distribution (optional)
      payload/
        app/index.js          the real esbuild CLI bundle
        app/package.json      declares external npm dependencies
        node_modules/         those dependencies, symlinks materialized
        assets/               server/ui dist, migrations, skills, runner vendor
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(SPECPATH).resolve()
BUILD_DIR = HERE / "build"
PAYLOAD_DIR = BUILD_DIR / "payload"
RUNTIME_DIR = BUILD_DIR / "runtime" / "node"

EXE_NAME = os.environ.get("PAPERCLIP_EXE_NAME", "paperclip")
EMBED_RUNTIME = os.environ.get("PAPERCLIP_EMBED_RUNTIME", "1") == "1"
ONEDIR = os.environ.get("PAPERCLIP_ONEDIR", "0") == "1"
VERSION_INFO = os.environ.get("PAPERCLIP_VERSION_INFO") or None
# The CLI is interactive: a windowed build would have no stdin/stdout and
# @clack/prompts would fail. Only opt out deliberately.
CONSOLE = os.environ.get("PAPERCLIP_CONSOLE", "1") == "1"

ICON_PATH = HERE / "assets" / "paperclip.ico"
ICON = str(ICON_PATH) if ICON_PATH.is_file() else None

if VERSION_INFO and not Path(VERSION_INFO).is_file():
    print(f"[spec] WARNING: PAPERCLIP_VERSION_INFO={VERSION_INFO} does not exist; ignoring")
    VERSION_INFO = None


def _walk_files(root: Path):
    """Yield files under root, excluding VCS/build noise and broken links."""
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
    skip_suffixes = {".pyc", ".pyo", ".log", ".tmp"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix in skip_suffixes:
            continue
        if path.is_symlink() and not path.exists():
            continue
        yield path


datas = []

# Application payload: the real, unmodified upstream build output.
if PAYLOAD_DIR.is_dir():
    for path in _walk_files(PAYLOAD_DIR):
        rel = path.relative_to(PAYLOAD_DIR).as_posix()
        datas.append((str(path), str(Path("payload") / Path(rel).parent)))
else:
    print(f"[spec] WARNING: no staged payload at {PAYLOAD_DIR} -- run build_exe.py --stage-payload")

# Portable Node runtime (only when --embed-node staged one).
if EMBED_RUNTIME and RUNTIME_DIR.is_dir():
    for path in _walk_files(RUNTIME_DIR):
        rel = path.relative_to(RUNTIME_DIR).as_posix()
        datas.append((str(path), str(Path("runtime") / "node" / Path(rel).parent)))
elif EMBED_RUNTIME:
    print(f"[spec] note: no staged runtime at {RUNTIME_DIR}; exe will use system Node")

a = Analysis(
    ["paperclip_exe/launcher.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "paperclip_exe",
        "paperclip_exe._platform",
        "paperclip_exe.payload",
        "paperclip_exe.postgres",
        "paperclip_exe.runner",
        "paperclip_exe.runtime",
        "paperclip_exe.signing",
        "paperclip_exe.staging",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is optional at runtime; exclude heavy unused packages.
    excludes=["tkinter", "unittest", "pydoc_data", "test", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# VERSIONINFO is a Windows-only resource; passing it elsewhere is noise.
if os.name != "nt" and VERSION_INFO:
    print("[spec] note: VERSIONINFO resource ignored on non-Windows targets")
    VERSION_INFO = None

# Shared EXE arguments.
_exe_kwargs = dict(
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX both corrupts node.exe / Postgres binaries and trips AV heuristics.
    upx=False,
    upx_exclude=[],
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("PAPERCLIP_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_INFO,
    # Do not require administrator privileges: Paperclip runs as a normal user.
    uac_admin=False,
    uac_uiaccess=False,
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_kwargs)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=EXE_NAME,
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **_exe_kwargs)
