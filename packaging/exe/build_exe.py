#!/usr/bin/env python3
"""Build a distributable Paperclip executable (``paperclip.exe`` on Windows).

The Paperclip app is Node.js/TypeScript. It is not rewritten -- the executable
*contains the real, unmodified build output* and a small Python launcher execs it
with inherited stdio. Node.js must be installed on the target machine (>= 24.11.0)
unless you build with ``--embed-node``, which bundles a portable runtime.

Why a launcher and not a Node SEA blob
--------------------------------------
Node's Single Executable Applications only accept a **CommonJS** main script.
The Paperclip CLI bundles to **ESM** and ~50 production source files depend on
``import.meta.url`` (see ``cli/src/commands/*``, ``cli/src/version.ts``), which
esbuild cannot faithfully lower to CJS. A SEA blob would ship a subtly broken
app. The launcher approach keeps the upstream bundle byte-for-byte intact.

Typical Windows build
---------------------
    py -3 packaging\\exe\\build_exe.py --stage-payload --freeze

Lower antivirus false positives (recommended for distribution)
-------------------------------------------------------------
    py -3 packaging\\exe\\build_exe.py --stage-payload --freeze --onedir

Stages
------
1. preflight   python, node, repo layout, minimum-version agreement
2. app build   esbuild bundle of the CLI (skippable if already built)
3. payload     stage app/ + node_modules/ + assets/, dereferencing symlinks
4. postgres    verify the embedded-postgres native runtime is staged
5. runtime     optional portable Node (--embed-node)
6. freeze      PyInstaller -> dist/paperclip[.exe] (+ sign on Windows)
7. verify      run the produced binary's self-check

Every stage is idempotent and individually skippable, so a partial environment
(no pnpm, no network) still gets you as far as it can.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paperclip_exe import MINIMUM_NODE_VERSION, __version__  # noqa: E402
from paperclip_exe._platform import (  # noqa: E402
    ARM64,
    LINUX,
    MACOS,
    WINDOWS,
    X64,
    PlatformInfo,
    current_platform,
    platform_from_keys,
)
from paperclip_exe.postgres import (  # noqa: E402
    NATIVE_SCOPE,
    REQUIRED_BINARIES,
    inspect as inspect_postgres,
    native_slug,
    precreate_linux_lib_aliases,
)
from paperclip_exe.runtime import (  # noqa: E402
    DEFAULT_NODE_VERSION,
    NodeRuntimeError,
    install_portable_node,
    node_dist_url,
    probe_node,
)
from paperclip_exe.signing import (  # noqa: E402
    SigningError,
    av_risk_report,
    generate_version_info,
    sign_binary,
    signing_config_from_env,
)
from paperclip_exe.staging import (  # noqa: E402
    StagingError,
    count_symlinks,
    ensure_executable_bits,
    merge_trees,
)

REPO_ROOT = HERE.parents[1]
BUILD_DIR = HERE / "build"
DIST_DIR = HERE / "dist"
PAYLOAD_DIR = BUILD_DIR / "payload"
RUNTIME_DIR = BUILD_DIR / "runtime"
SPEC_PATH = HERE / "paperclip_exe.spec"
VERSION_INFO_PATH = BUILD_DIR / "version_info.txt"

#: Where the repo declares the minimum Node version (source of truth).
NODE_VERSION_TS = REPO_ROOT / "packages" / "shared" / "src" / "node-version.ts"

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #


class BuildError(RuntimeError):
    """Fatal build failure with a user-facing message."""


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def note(status: str, message: str) -> None:
    print(f"  [{status}] {message}", flush=True)


def run(
    command: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run a command, echoing it, and raise BuildError on failure when check=True."""
    printable = " ".join(str(part) for part in command)
    print(f"  $ {printable}" + (f"   (cwd={cwd})" if cwd else ""), flush=True)
    merged: Dict[str, str] = dict(os.environ)
    if env:
        merged.update(env)
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            env=merged,
            capture_output=capture,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        if check:
            raise BuildError(f"required tool not found: {exc}") from exc
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))
    if capture and completed.stdout:
        for line in completed.stdout.strip().splitlines()[-20:]:
            print(f"    | {line}", flush=True)
    if check and completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-800:] if capture else ""
        raise BuildError(f"command failed ({completed.returncode}): {printable}\n{detail}")
    return completed


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# --------------------------------------------------------------------------- #
# Stage 1: preflight
# --------------------------------------------------------------------------- #


def read_repo_minimum_node() -> Optional[str]:
    """Parse MINIMUM_NODE_VERSION out of the repo's TypeScript source of truth."""
    if not NODE_VERSION_TS.is_file():
        return None
    match = re.search(
        r'MINIMUM_NODE_VERSION\s*=\s*["\'](\d+\.\d+\.\d+)["\']',
        NODE_VERSION_TS.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def read_app_version() -> str:
    """Application version, for the Windows VERSIONINFO resource."""
    pkg = REPO_ROOT / "cli" / "package.json"
    if pkg.is_file():
        try:
            return str(json.loads(pkg.read_text(encoding="utf-8")).get("version") or "0.0.0")
        except (OSError, json.JSONDecodeError):
            pass
    return "0.0.0"


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def node_version_text(node_bin: Optional[str] = None) -> Optional[str]:
    binary = node_bin or which("node")
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def parse_version(text: str) -> Tuple[int, int, int]:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def stage_preflight(args: argparse.Namespace, plat: PlatformInfo) -> None:
    step("Stage 1/7  preflight")

    if sys.version_info < (3, 9):
        raise BuildError(f"Python >= 3.9 required to build; found {sys.version.split()[0]}")
    note(OK, f"python {sys.version.split()[0]}")
    note(OK, f"target platform: {plat.describe()}")
    note(OK, f"repo root: {REPO_ROOT}")

    repo_minimum = read_repo_minimum_node()
    if repo_minimum is None:
        note(WARN, f"could not read {NODE_VERSION_TS.relative_to(REPO_ROOT)}; assuming {MINIMUM_NODE_VERSION}")
    elif repo_minimum != MINIMUM_NODE_VERSION:
        note(WARN, f"repo requires Node {repo_minimum} but this pipeline pins {MINIMUM_NODE_VERSION}")
        note(WARN, "update MINIMUM_NODE_VERSION in packaging/exe/paperclip_exe/__init__.py")
        if not args.allow_version_mismatch:
            raise BuildError("minimum Node version mismatch (pass --allow-version-mismatch to override)")
    else:
        note(OK, f"minimum Node version agrees with repo: {repo_minimum}")

    required_minimum = repo_minimum or MINIMUM_NODE_VERSION

    if args.embed_node:
        if parse_version(args.node_version.lstrip("v")) < parse_version(required_minimum):
            raise BuildError(
                f"--node-version {args.node_version} is below the required {required_minimum}; "
                "set PAPERCLIP_NODE_VERSION or pass a newer --node-version"
            )
        note(OK, f"will embed portable Node {args.node_version} from {node_dist_url(args.node_version, plat)}")
    else:
        # The exe will rely on Node already installed on the target machine, so
        # fail loudly now if the host we are testing against cannot satisfy it.
        local = node_version_text()
        if local is None:
            note(WARN, "no Node on PATH; the built exe will require Node >= " + required_minimum + " at runtime")
        elif parse_version(local) < parse_version(required_minimum):
            note(FAIL, f"host node {local} is BELOW the required {required_minimum}")
            note(WARN, "the exe will refuse to run on machines like this one")
            note(WARN, "either upgrade Node, or build with --embed-node to bundle a runtime")
            if not args.allow_old_node and not args.dry_run:
                raise BuildError(
                    f"host Node {local} < required {required_minimum}. "
                    "Install Node 24.11.0+ from https://nodejs.org/, or pass --embed-node, "
                    "or --allow-old-node to build anyway."
                )
        else:
            note(OK, f"host node {local} satisfies >= {required_minimum} (exe will use system Node)")

    if not args.skip_app_build:
        pnpm = which("pnpm") or which("corepack")
        note(OK if pnpm else WARN, f"pnpm/corepack: {pnpm or 'not found (app build may fail)'}")

    if args.freeze and not args.dry_run:
        pyinstaller = _find_pyinstaller()
        note(OK if pyinstaller else FAIL, f"pyinstaller: {pyinstaller or 'not installed -- pip install pyinstaller'}")
        if not pyinstaller:
            raise BuildError("PyInstaller is required to produce the executable (pip install pyinstaller)")

    if args.sign:
        config = signing_config_from_env()
        note(OK, f"signing requested via {config.method}" + ("" if config.enabled else " (no credential set)"))
        if not config.enabled:
            note(WARN, "--sign passed but no PAPERCLIP_SIGN_SUBJECT / PAPERCLIP_PFX / PAPERCLIP_SIGN_SHA1 set")


def _find_pyinstaller() -> Optional[str]:
    exe = which("pyinstaller")
    if exe:
        return exe
    probe = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return f"{sys.executable} -m PyInstaller" if probe.returncode == 0 else None


# --------------------------------------------------------------------------- #
# Stage 2: application build (esbuild bundle)
# --------------------------------------------------------------------------- #


def cli_bundle_path() -> Path:
    return REPO_ROOT / "cli" / "dist" / "index.js"


def stage_app_build(args: argparse.Namespace) -> None:
    step("Stage 2/7  application build (esbuild bundle)")

    if args.skip_app_build:
        if cli_bundle_path().is_file():
            note(SKIP, f"--skip-app-build; using existing {cli_bundle_path().relative_to(REPO_ROOT)}")
        else:
            note(WARN, "--skip-app-build and no bundle exists; payload staging will fail")
        return

    if not _resolve_node_modules() and not args.allow_missing_deps and not args.dry_run:
        raise BuildError(
            "dependencies are not installed in this checkout.\n"
            "  Run 'pnpm install' first, or pass --skip-app-build to package an\n"
            "  existing cli/dist, or --allow-missing-deps to continue anyway."
        )

    package_manager = which("pnpm")
    if package_manager:
        run([package_manager, "--filter", "paperclipai", "build"], cwd=REPO_ROOT, check=not args.dry_run)
    else:
        note(WARN, "pnpm unavailable; invoking esbuild directly")
        node = which("node")
        if not node and not args.dry_run:
            raise BuildError("neither pnpm nor node was found; cannot build the CLI bundle")
        run(
            [
                node or "node",
                "--input-type=module",
                "-e",
                "import esbuild from 'esbuild'; import config from './esbuild.config.mjs'; await esbuild.build(config);",
            ],
            cwd=REPO_ROOT / "cli",
            check=not args.dry_run,
        )

    if cli_bundle_path().is_file():
        note(OK, f"bundle built: {human_size(cli_bundle_path().stat().st_size)}")
    elif not args.dry_run:
        raise BuildError(f"app build finished but {cli_bundle_path()} does not exist")


# --------------------------------------------------------------------------- #
# Stage 3: payload staging
# --------------------------------------------------------------------------- #


@dataclass
class StagedPayload:
    root: Path
    entry: Path
    node_modules: Optional[Path]
    warnings: List[str]


def _resolve_node_modules() -> List[Path]:
    """Existing node_modules roots that may hold the CLI's external deps."""
    return [
        p
        for p in (
            REPO_ROOT / "cli" / "node_modules",
            REPO_ROOT / "node_modules",
        )
        if p.is_dir()
    ]


def _asset_sources() -> List[Tuple[str, Path]]:
    """Runtime assets the server/CLI read from disk, in staging order."""
    return [
        ("server/dist", REPO_ROOT / "server" / "dist"),
        ("server/ui-dist", REPO_ROOT / "server" / "ui-dist"),
        ("server/skills", REPO_ROOT / "server" / "skills"),
        ("db/migrations", REPO_ROOT / "packages" / "db" / "src" / "migrations"),
        ("skills", REPO_ROOT / "skills"),
        ("runner-vendor", REPO_ROOT / "packages" / "paperclip-runner" / "dist"),
    ]


def _copy_tree(src: Path, dst: Path, *, label: str) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    note(OK, f"assets/{label}")
    return True


def _hoist_embedded_postgres_packages(staged_node_modules: Path, original_roots: List[Path]) -> int:
    """Ensure @embedded-postgres/* packages are siblings of embedded-postgres.

    The app resolves native packages via:
        path.resolve(packageRoot, \"..\", \"@embedded-postgres\", slug)
    So node_modules must contain:
        embedded-postgres/
        @embedded-postgres/<slug>/

    pnpm nests the native package inside embedded-postgres's own node_modules,
    and also in the central .pnpm store. This function finds any native
    packages anywhere in the staged tree or original roots and copies them to
    the top-level @embedded-postgres scope.

    Returns number of packages hoisted.
    """
    hoisted = 0
    # Search locations for native packages:
    # 1. Inside already-staged tree (e.g. staged/embedded-postgres/node_modules/@embedded-postgres/*)
    # 2. Inside original pnpm store (.pnpm/@embedded-postgres+*/node_modules/@embedded-postgres/*)
    candidates: List[Path] = []

    # From staged tree
    for path in staged_node_modules.rglob("@embedded-postgres"):
        if not path.is_dir():
            continue
        # path is .../@embedded-postgres, its children are slugs
        for slug_dir in path.iterdir():
            if not slug_dir.is_dir():
                continue
            if (slug_dir / "package.json").is_file():
                candidates.append(slug_dir)

    # From original roots' pnpm store
    for root in original_roots:
        pnpm_store = root.parent / ".pnpm" if (root.parent / ".pnpm").is_dir() else root / ".pnpm"
        # Also check repo root .pnpm
        for store in [pnpm_store, REPO_ROOT / "node_modules" / ".pnpm"]:
            if not store.is_dir():
                continue
            for entry in store.iterdir():
                if not entry.name.startswith("@embedded-postgres"):
                    continue
                # entry is like @embedded-postgres+linux-x64@...
                native = entry / "node_modules" / "@embedded-postgres"
                if not native.is_dir():
                    continue
                for slug_dir in native.iterdir():
                    if (slug_dir / "package.json").is_file():
                        candidates.append(slug_dir)

    # Deduplicate by slug name, preferring already-staged or first found
    seen_slugs: Dict[str, Path] = {}
    for cand in candidates:
        slug = cand.name
        if slug not in seen_slugs:
            seen_slugs[slug] = cand

    target_scope = staged_node_modules / "@embedded-postgres"
    target_scope.mkdir(parents=True, exist_ok=True)

    for slug, src in seen_slugs.items():
        dst = target_scope / slug
        if dst.is_dir():
            # Already present and healthy, skip
            continue
        try:
            shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
            hoisted += 1
        except OSError:
            continue

    # Also ensure embedded-postgres wrapper itself is at top level (it should be)
    # If it's nested, hoist it too
    if not (staged_node_modules / "embedded-postgres").is_dir():
        for cand in candidates:
            # Look for wrapper near native package: ../../embedded-postgres
            wrapper = cand.parent.parent.parent / "embedded-postgres"
            if wrapper.is_dir() and (wrapper / "package.json").is_file():
                try:
                    shutil.copytree(wrapper, staged_node_modules / "embedded-postgres", symlinks=False)
                    hoisted += 1
                except OSError:
                    pass
                break

    return hoisted


def _hoist_all_external_deps(staged_node_modules: Path) -> int:
    """Hoist all transitive external deps from pnpm store to flat node_modules.

    The CLI bundle (app/index.js) is ESM with external deps like 'zod',
    'commander', etc. Node's ESM resolver looks for node_modules beside the
    entry file (payload/app/node_modules) then parent (payload/node_modules).
    pnpm's default structure keeps transitive deps nested in .pnpm store, not
    flat, so they are not found unless we hoist them.

    This runs `pnpm --filter paperclipai list --depth=Infinity --parseable`
    to get all package paths in the dependency graph, then copies any package
    not already present at top level into the flat node_modules.

    Returns number of packages hoisted.
    """
    hoisted = 0
    try:
        result = subprocess.run(
            ["pnpm", "--filter", "paperclipai", "list", "--depth=Infinity", "--parseable"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    if result.returncode != 0:
        return 0

    seen: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == str(REPO_ROOT / "cli"):
            continue
        p = Path(line)
        parts = p.parts
        try:
            nm_idx = len(parts) - 1 - parts[::-1].index("node_modules")
        except ValueError:
            continue
        pkg_parts = parts[nm_idx + 1 :]
        if not pkg_parts:
            continue
        pkg_name = "/".join(pkg_parts)
        if pkg_name.startswith("@paperclipai/"):
            continue
        if pkg_name not in seen:
            seen[pkg_name] = line

    for pkg_name, src_path in seen.items():
        dst = staged_node_modules / pkg_name
        if dst.exists():
            continue
        # Skip optional platform-specific packages that are not for current OS
        # except the one matching current platform - they are handled separately
        # but we still want to hoist the current platform's native package
        # which is already handled by _hoist_embedded_postgres, so skip all
        # @embedded-postgres/* here to avoid double work
        if pkg_name.startswith("@embedded-postgres/"):
            continue
        # Skip heavy optional native bindings that are not needed for CLI
        # (they are large and platform-specific)
        if pkg_name.startswith("@esbuild/") and "linux" not in pkg_name and "x64" not in pkg_name:
            # Keep only linux-x64 for current platform, skip others to save space
            # Actually keep all esbuild platforms? They are small, but we can skip
            # non-linux to reduce size. For Windows build, we need windows-x64.
            # For now, only skip if not matching current platform would be complex,
            # so keep all to be safe - comment out skip
            pass

        try:
            shutil.copytree(src_path, dst, symlinks=False, ignore_dangling_symlinks=True)
            hoisted += 1
        except OSError:
            continue

    return hoisted


def stage_payload(args: argparse.Namespace, plat: PlatformInfo) -> Optional[StagedPayload]:
    step("Stage 3/7  payload staging")

    bundle = cli_bundle_path()
    modules = _resolve_node_modules()

    if args.dry_run and args.stage_payload:
        note(SKIP, "dry run: nothing written")
        note(
            OK if bundle.is_file() else FAIL,
            f"app/index.js would come from {bundle.relative_to(REPO_ROOT)}"
            + (f" ({human_size(bundle.stat().st_size)})" if bundle.is_file() else " (MISSING)"),
        )
        note(OK, "app/package.json would come from cli/package.json")
        if modules:
            note(OK, f"node_modules would be materialized from {modules[0].relative_to(REPO_ROOT)}")
            note(OK, "  (symlinks dereferenced -- required for Windows and for freezing)")
        else:
            note(FAIL, "node_modules unavailable -- run 'pnpm install' first")
        available = [label for label, src in _asset_sources() if src.exists()]
        note(OK if available else WARN, f"assets available: {', '.join(available) or 'none'}")
        return None

    if not args.stage_payload:
        if PAYLOAD_DIR.is_dir():
            entry = PAYLOAD_DIR / "app" / "index.js"
            node_modules = PAYLOAD_DIR / "node_modules"
            note(SKIP, f"reusing previously staged {PAYLOAD_DIR}")
            return StagedPayload(
                root=PAYLOAD_DIR,
                entry=entry,
                node_modules=node_modules if node_modules.is_dir() else None,
                warnings=[],
            )
        note(SKIP, "--stage-payload not passed and nothing staged yet")
        return None

    warnings: List[str] = []
    if PAYLOAD_DIR.exists():
        shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
    app_dir = PAYLOAD_DIR / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    # 1. The esbuild bundle -- the actual application.
    if bundle.is_file():
        shutil.copy2(bundle, app_dir / "index.js")
        sourcemap = bundle.with_suffix(".js.map")
        if sourcemap.is_file():
            shutil.copy2(sourcemap, app_dir / "index.js.map")
        note(OK, f"app/index.js ({human_size(bundle.stat().st_size)})")
    else:
        warnings.append(f"CLI bundle not found at {bundle}")
        note(FAIL, "CLI bundle missing -- run without --skip-app-build")

    # 2. package.json so the bundle's external deps resolve as a normal package.
    # The bundle's version.ts does require("../package.json") from app/index.js,
    # which resolves to payload/package.json, so we need it in both places.
    cli_pkg = REPO_ROOT / "cli" / "package.json"
    if cli_pkg.is_file():
        shutil.copy2(cli_pkg, app_dir / "package.json")
        shutil.copy2(cli_pkg, PAYLOAD_DIR / "package.json")
        note(OK, f"app/package.json + payload/package.json (paperclipai {read_app_version()})")
    else:
        warnings.append(f"cli/package.json not found at {cli_pkg}")

    # 3. node_modules holding the externals. Materialized, never symlinked:
    #    Windows cannot create symlinks without privilege, PyInstaller does not
    #    preserve them, and embedded-postgres resolves its native binaries as a
    #    sibling of its own package dir -- which only holds in a flat tree.
    modules_dst = PAYLOAD_DIR / "node_modules"
    if modules:
        try:
            stats = merge_trees(modules, modules_dst)
            note(OK, f"node_modules materialized: {stats.describe()}")
            remaining = count_symlinks(modules_dst)
            if remaining:
                note(WARN, f"{remaining} symlinks remain in the staged tree (may break on Windows)")
                warnings.append(f"{remaining} symlinks survived staging")
            else:
                note(OK, "no symlinks remain (safe for Windows and for freezing)")

            # --- Fix embedded-postgres sibling layout ---------------------------------
            # pnpm nests the native packages inside embedded-postgres's own
            # node_modules (e.g. embedded-postgres/node_modules/@embedded-postgres/linux-x64).
            # The app resolves them as siblings of embedded-postgres, so we must
            # hoist any @embedded-postgres/* found anywhere in the staged tree
            # or in the original pnpm store up to the top-level node_modules.
            # This is the critical fix for the "embedded postgres problem".
            hoisted_pg = _hoist_embedded_postgres_packages(modules_dst, modules)
            if hoisted_pg:
                note(OK, f"hoisted {hoisted_pg} @embedded-postgres native packages to sibling layout")

            # --- Hoist all transitive external deps ------------------------------------
            # The ESM bundle has external deps (zod, etc.) that pnpm keeps nested
            # in .pnpm store. Node's ESM resolver needs them flat beside the bundle.
            # This fixes "Cannot find package 'zod'" errors in frozen builds.
            hoisted_all = _hoist_all_external_deps(modules_dst)
            if hoisted_all:
                note(OK, f"hoisted {hoisted_all} transitive external deps to flat node_modules")
        except StagingError as exc:
            raise BuildError(f"could not stage node_modules: {exc}") from exc
    else:
        warnings.append("no node_modules found; external npm dependencies will not resolve")
        note(FAIL, "no node_modules in this checkout -- run 'pnpm install' first")

    # 4. Assets the server/CLI read from disk at runtime.
    assets_dst = PAYLOAD_DIR / "assets"
    assets_dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for label, src in _asset_sources():
        if _copy_tree(src, assets_dst / label.replace("/", os.sep), label=label):
            copied += 1
    total_assets = len(_asset_sources())
    if copied:
        note(OK, f"assets staged ({copied} of {total_assets} available sources)")
    else:
        note(WARN, "no built assets found (server/dist, migrations, ...); server mode may be limited")
        warnings.append("no built assets were staged")

    staged = StagedPayload(
        root=PAYLOAD_DIR,
        entry=app_dir / "index.js",
        node_modules=modules_dst if modules_dst.is_dir() else None,
        warnings=warnings,
    )
    if staged.entry.is_file():
        note(OK, f"payload total: {human_size(dir_size(PAYLOAD_DIR))}")
    return staged


# --------------------------------------------------------------------------- #
# Stage 4: embedded Postgres native runtime
# --------------------------------------------------------------------------- #


def stage_postgres(args: argparse.Namespace, payload: Optional[StagedPayload], plat: PlatformInfo) -> bool:
    """Verify (and where possible repair) the embedded Postgres native runtime.

    Returns True when embedded Postgres should work offline on the target.
    """
    step("Stage 4/7  embedded Postgres native runtime")

    slug = native_slug(plat)
    if slug is None:
        note(WARN, f"no published @embedded-postgres build for {plat.os}/{plat.arch}")
        note(WARN, "embedded Postgres cannot run there; configure an external DATABASE_URL")
        return False

    if payload is None or payload.node_modules is None:
        note(FAIL, f"no staged node_modules to inspect for {NATIVE_SCOPE}/{slug}")
        return False

    status = inspect_postgres(payload.node_modules, plat)
    if status.healthy:
        note(OK, status.describe())
    else:
        note(WARN, status.describe())
        for line in status.notes:
            note(WARN, f"  {line}")

    # Repairs we can actually perform on the staged tree:

    # a) The app creates libX.so.A aliases at runtime, needing a writable lib
    #    dir. Pre-create them as real copies so no runtime symlink is required
    #    (PyInstaller archives do not preserve symlinks).
    if plat.os == LINUX and status.lib_dir is not None:
        created = precreate_linux_lib_aliases(status.lib_dir)
        if created:
            note(OK, f"pre-created {created} Linux .so aliases in native/lib")
        else:
            note(OK, "native/lib aliases already present (or none needed)")

    # b) Copying can drop the executable bit, which turns initdb into EACCES.
    if status.package_dir is not None:
        bin_dir = status.package_dir / "native" / "bin"
        if bin_dir.is_dir():
            names = [f"{name}{plat.exe_suffix}" for name in REQUIRED_BINARIES]
            fixed = ensure_executable_bits(bin_dir, names)
            if fixed:
                note(OK, f"restored executable bit on: {', '.join(sorted(set(fixed)))}")

    refreshed = inspect_postgres(payload.node_modules, plat)
    if not refreshed.healthy and not args.allow_incomplete_postgres and not args.dry_run:
        note(WARN, "embedded Postgres is incomplete in this build")
        note(WARN, "the exe still runs; only 'onboard'/'run' needing a local DB are affected")
        note(WARN, "fix: reinstall deps on this OS/arch (pnpm install), or set DATABASE_URL")
        if args.require_postgres:
            raise BuildError(
                f"{NATIVE_SCOPE}/{slug} is not usable in the staged payload. "
                "Run 'pnpm install' on this platform, or drop --require-postgres."
            )
    return refreshed.healthy


# --------------------------------------------------------------------------- #
# Stage 5: portable runtime (optional)
# --------------------------------------------------------------------------- #


def stage_runtime(args: argparse.Namespace, plat: PlatformInfo) -> Optional[Path]:
    step("Stage 5/7  Node runtime")

    if not args.embed_node:
        note(SKIP, "--embed-node not set; the exe will use Node installed on the machine")
        note(SKIP, f"target machines need Node >= {read_repo_minimum_node() or MINIMUM_NODE_VERSION}")
        return None

    if args.dry_run:
        note(SKIP, f"dry run: would fetch {node_dist_url(args.node_version, plat)}")
        return None

    target = RUNTIME_DIR / f"node-{plat.os}-{plat.arch}"
    try:
        node_bin = install_portable_node(target, plat=plat, version=args.node_version)
    except NodeRuntimeError as exc:
        raise BuildError(str(exc)) from exc

    version = probe_node(node_bin)
    note(OK, f"portable Node {version} downloaded")

    staged = RUNTIME_DIR / "node"
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(target, staged, symlinks=False)
    final = staged / plat.node_bin_relpath
    if not final.is_file():
        raise BuildError(f"staged runtime is missing its binary at {final}")
    note(OK, f"runtime staged for freeze ({human_size(dir_size(staged))})")
    return staged


# --------------------------------------------------------------------------- #
# Stage 6: freeze with PyInstaller (+ sign on Windows)
# --------------------------------------------------------------------------- #


def stage_freeze(args: argparse.Namespace, plat: PlatformInfo) -> Optional[Path]:
    step("Stage 6/7  freeze (PyInstaller " + ("onedir" if args.onedir else "onefile") + ")")

    if not args.freeze:
        note(SKIP, "--no-freeze passed; staged payload/runtime are ready to freeze later")
        return None
    if args.dry_run:
        note(SKIP, "dry run: would invoke PyInstaller")
        return None

    pyinstaller = _find_pyinstaller()
    if not pyinstaller:
        raise BuildError("PyInstaller not found; run: pip install pyinstaller")

    exe_name = args.name or f"paperclip{plat.exe_suffix}"
    stem = Path(exe_name).stem

    # Windows version metadata: a cheap, real AV mitigation. Binaries with no
    # VERSIONINFO are treated as suspicious by many engines.
    version_info_exists = False
    if plat.os == WINDOWS and not args.no_version_resource:
        generate_version_info(
            VERSION_INFO_PATH,
            version=read_app_version(),
            exe_name=Path(exe_name).name,
        )
        version_info_exists = True
        note(OK, f"VERSIONINFO resource generated ({read_app_version()})")

    command: List[str] = pyinstaller.split(" ") if " " in pyinstaller else [pyinstaller]
    command += [
        str(SPEC_PATH),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
    ]
    env = {
        "PAPERCLIP_EXE_NAME": stem,
        "PAPERCLIP_EMBED_RUNTIME": "1" if (RUNTIME_DIR / "node").is_dir() else "0",
        "PAPERCLIP_ONEDIR": "1" if args.onedir else "0",
        "PAPERCLIP_VERSION_INFO": str(VERSION_INFO_PATH) if version_info_exists else "",
        "PAPERCLIP_CONSOLE": "0" if args.windowed else "1",
    }
    run(command, cwd=HERE, env=env)

    produced = DIST_DIR / exe_name
    if not produced.exists():
        candidates = sorted(p for p in DIST_DIR.iterdir() if p.name.startswith(stem)) if DIST_DIR.is_dir() else []
        if len(candidates) == 1:
            produced = candidates[0]
        else:
            raise BuildError(f"PyInstaller finished but {produced} was not produced")

    if produced.is_dir():
        binary = produced / exe_name
        if not binary.is_file():
            raise BuildError(f"onedir bundle produced but {binary} is missing")
        note(OK, f"built {produced} ({human_size(dir_size(produced))} onedir)")
    else:
        binary = produced
        note(OK, f"built {binary} ({human_size(binary.stat().st_size)})")

    note(OK, f"sha256 {sha256_of(binary)}")

    # ---- signing -------------------------------------------------------
    config = signing_config_from_env()
    signed = False
    if args.sign or config.enabled:
        if plat.os != WINDOWS:
            note(WARN, "Authenticode signing applies to Windows targets; skipped here")
        else:
            try:
                summary = sign_binary(binary, config)
                if summary:
                    signed = True
                    note(OK, summary)
            except SigningError as exc:
                if args.allow_unsigned:
                    note(WARN, str(exc))
                else:
                    raise BuildError(str(exc)) from exc

    args._signed = signed  # consumed by the summary
    args._version_info = version_info_exists
    return binary


# --------------------------------------------------------------------------- #
# Stage 7: verify
# --------------------------------------------------------------------------- #


def stage_verify(args: argparse.Namespace, binary: Optional[Path]) -> None:
    step("Stage 7/7  verify")

    if binary is None:
        note(SKIP, "no binary produced; verifying the launcher from source instead")
        if args.dry_run:
            note(SKIP, "dry run")
            return
        completed = subprocess.run(
            [sys.executable, "-m", "paperclip_exe.launcher", "--launcher-info"],
            cwd=str(HERE),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            note(OK, "launcher module imports and reports info")
        else:
            note(WARN, f"launcher --launcher-info exited {completed.returncode}")
            if completed.stderr:
                print(completed.stderr.strip()[-600:])
        return

    started = time.time()
    completed = subprocess.run([str(binary), "--launcher-info"], capture_output=True, text=True, check=False)
    elapsed = time.time() - started
    if completed.returncode != 0:
        note(FAIL, f"--launcher-info exited {completed.returncode}")
        print((completed.stdout or "")[-800:])
        print((completed.stderr or "")[-800:])
        raise BuildError("produced executable failed its self-check")
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError:
        note(WARN, "self-check output was not valid JSON")
        info = {}
    note(OK, f"self-check passed in {elapsed:.2f}s")
    note(OK if info.get("node") else WARN, f"node: {info.get('node') or 'NOT RESOLVED'}")
    payload_info = info.get("payload") or {}
    note(OK if payload_info else WARN, f"payload entry: {payload_info.get('entry') or 'NOT RESOLVED'}")

    postgres = info.get("embedded_postgres") or {}
    if postgres:
        note(
            OK if postgres.get("healthy") else WARN,
            f"embedded postgres: {postgres.get('slug') or 'n/a'} "
            f"({'healthy' if postgres.get('healthy') else 'incomplete'})",
        )

    if args.run_doctor:
        doctor = subprocess.run([str(binary), "--launcher-doctor"], check=False)
        note(OK if doctor.returncode == 0 else WARN, f"--launcher-doctor exit {doctor.returncode}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_exe.py",
        description="Build a Paperclip executable containing the real app.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # standalone Windows build: Node is included for the target\n"
            "  py -3 packaging\\exe\\build_exe.py --stage-payload --embed-node --freeze --onedir\n\n"
            "  # development-only build using Node already installed on the target\n"
            "  py -3 packaging\\exe\\build_exe.py --stage-payload --system-node --freeze --onedir\n\n"
            "  # package an already-built cli/dist without rebuilding\n"
            "  python3 packaging/exe/build_exe.py --skip-app-build --stage-payload\n\n"
            "  # validate the pipeline only (writes nothing)\n"
            "  python3 packaging/exe/build_exe.py --dry-run --all --skip-app-build\n"
        ),
    )
    parser.add_argument("--all", action="store_true", help="enable --stage-payload and --freeze")
    parser.add_argument("--stage-payload", action="store_true", help="stage app/, node_modules/ and assets/")
    runtime_group = parser.add_mutually_exclusive_group()
    runtime_group.add_argument("--embed-node", dest="embed_node", action="store_true", help="bundle a portable Node runtime inside the exe (standalone distribution)")
    runtime_group.add_argument("--system-node", dest="embed_node", action="store_false", help="use Node installed on the target machine (development-only distribution)")
    parser.set_defaults(embed_node=False)
    parser.add_argument("--freeze", action="store_true", help="run PyInstaller to produce the executable")
    parser.add_argument("--no-freeze", dest="freeze", action="store_false", help="skip PyInstaller")
    parser.set_defaults(freeze=True)
    parser.add_argument("--skip-app-build", action="store_true", help="do not run the esbuild CLI bundle")
    parser.add_argument("--onedir", action="store_true", default=True,
                        help="build a folder instead of a self-extracting file (far fewer AV false positives) [DEFAULT]")
    parser.add_argument("--onefile", dest="onedir", action="store_false",
                        help="build a single self-extracting file (more AV false positives, but single file)")
    parser.add_argument("--windowed", action="store_true",
                        help="build without a console (NOT recommended: the CLI is interactive)")
    parser.add_argument("--sign", action="store_true", help="Authenticode-sign the result (needs a certificate)")
    parser.add_argument("--allow-unsigned", action="store_true", help="continue if signing fails")
    parser.add_argument("--no-version-resource", action="store_true", help="skip the Windows VERSIONINFO resource")
    parser.add_argument("--node-version", default=DEFAULT_NODE_VERSION, help=f"Node to embed (default {DEFAULT_NODE_VERSION})")
    parser.add_argument("--name", default=None, help="output executable name (default: paperclip[.exe])")
    parser.add_argument("--target-os", default=None, choices=[WINDOWS, MACOS, LINUX], help="override target OS for staging")
    parser.add_argument("--target-arch", default=None, choices=[X64, ARM64], help="override target arch for staging")
    parser.add_argument("--run-doctor", action="store_true", help="also run --launcher-doctor on the produced binary")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen; change nothing")
    parser.add_argument("--allow-missing-deps", action="store_true", help="continue when node_modules is absent")
    parser.add_argument("--allow-old-node", action="store_true", help="build even if host Node is below the minimum")
    parser.add_argument("--allow-incomplete-postgres", action="store_true", help="do not warn loudly about Postgres")
    parser.add_argument("--require-postgres", action="store_true", help="fail the build if embedded Postgres is incomplete")
    parser.add_argument("--allow-version-mismatch", action="store_true", help="continue when the Node pin disagrees with the repo")
    parser.add_argument("--version", action="version", version=f"paperclip-exe-builder {__version__}")
    return parser


def resolve_target_platform(args: argparse.Namespace) -> PlatformInfo:
    host = current_platform()
    os_key = args.target_os or host.os
    arch = args.target_arch or host.arch
    if os_key == host.os and arch == host.arch:
        return host
    return platform_from_keys(os_key, arch)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.all:
        args.stage_payload = True
        args.freeze = True
    args._signed = False
    args._version_info = False

    plat = resolve_target_platform(args)
    cross = plat.os != current_platform().os or plat.arch != current_platform().arch

    print(f"Paperclip executable builder {__version__}")
    print(f"  repo   : {REPO_ROOT}")
    print(f"  target : {plat.os}/{plat.arch}" + ("  (cross-target: staging only)" if cross else ""))
    print(f"  layout : {'onedir (folder)' if args.onedir else 'onefile (single binary)'}")
    print(f"  node   : {'embedded portable runtime' if args.embed_node else 'system Node (required on target)'}")

    if cross and args.freeze and not args.dry_run:
        note(
            WARN,
            "PyInstaller cannot cross-compile: the .exe must be built on Windows, "
            "a Mach-O binary on macOS, and an ELF binary on Linux.",
        )
        args.freeze = False

    try:
        stage_preflight(args, plat)
        stage_app_build(args)
        payload = stage_payload(args, plat)
        postgres_ok = stage_postgres(args, payload, plat)
        stage_runtime(args, plat)
        binary = stage_freeze(args, plat)
        stage_verify(args, binary)
    except BuildError as exc:
        print(f"\n[{FAIL}] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print("\n" + "=" * 64)
    if binary is not None:
        print(f"DONE  {binary}")
        if binary.is_dir():
            print(f"      layout onedir  {human_size(dir_size(binary))}")
        else:
            print(f"      size   {human_size(binary.stat().st_size)}")
            print(f"      sha256 {sha256_of(binary)}")
        print(f"\nRun it:  {binary.name} --launcher-doctor")

        report = av_risk_report(
            signed=bool(args._signed),
            onefile=not args.onedir,
            has_version_info=bool(args._version_info),
            plat=plat,
        )
        print("\nAntivirus / SmartScreen posture")
        for line in report.lines():
            print(line)
    else:
        print("DONE  staging complete (no executable produced)")
        if payload is not None:
            print(f"      payload: {payload.root}")
            for warning in payload.warnings:
                print(f"      warn   : {warning}")
        print(f"      embedded postgres: {'ready' if postgres_ok else 'incomplete'}")
        print("\nNext: re-run with --freeze on the target OS to produce the binary.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
