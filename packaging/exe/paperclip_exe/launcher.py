"""Entry point for the frozen ``paperclip`` executable.

Design rule: **every argument that is not a ``--launcher-*`` flag is forwarded
verbatim.** The launcher never interprets Paperclip's own CLI grammar, so new
upstream commands work without touching this file.

Built-in launcher controls
--------------------------
``--launcher-info``        Print resolved runtime/payload as JSON and exit 0.
``--launcher-doctor``      Diagnose the installation (Node, payload, data dir).
``--launcher-fetch-node``  Allow downloading a portable Node if none qualifies.
``--launcher-version``     Print launcher + app versions and exit 0.
``--launcher-help``        This help text.

Set ``PAPERCLIP_LAUNCHER_DEBUG=1`` to trace resolution decisions on stderr.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import MINIMUM_NODE_VERSION, __version__
from ._platform import app_root, bundled_root, current_platform, env_flag, env_path
from .payload import Payload, PayloadError, data_dir, find_payload, payload_manifest
from .postgres import darwin_path_entries
from .postgres import doctor_lines as postgres_doctor_lines
from .postgres import inspect as inspect_postgres
from .postgres import linux_lib_path_entries
from .postgres import windows_path_entries
from .runner import (
    LAUNCHER_ERROR_EXIT,
    build_command,
    build_environment,
    run_app,
)
from .runtime import NodeRuntime, NodeRuntimeError, resolve_node

#: Prefix that marks a flag as belonging to the launcher, never the app.
LAUNCHER_FLAG_PREFIX = "--launcher-"

#: Launcher flags that take a value. The value may be given inline
#: (``--launcher-node=/path``) or as the next token (``--launcher-node /path``);
#: in the latter case the value token must be consumed here rather than being
#: mistaken for the app's first argument.
LAUNCHER_FLAGS_WITH_VALUE = frozenset({"--launcher-node", "--launcher-payload"})

LAUNCHER_HELP = f"""paperclip launcher {__version__}

Runs the real Paperclip CLI/app from a single self-contained executable.

Usage
  paperclip [launcher-flag] [--] <any paperclipai arguments...>

Launcher flags (consumed here, not forwarded)
  --launcher-info          Print resolved Node runtime and payload as JSON.
  --launcher-doctor        Diagnose this installation and report problems.
  --launcher-fetch-node    Permit downloading a portable Node if none qualifies.
  --launcher-node PATH     Use PATH as the Node binary (one-shot override).
  --launcher-payload PATH  Use PATH as the payload root (one-shot override).
  --launcher-version       Print launcher and app versions.
  --launcher-help          Show this help.

Everything after the launcher flags (or after a bare `--`) is passed through to
Paperclip unchanged, e.g.:

  paperclip doctor
  paperclip onboard
  paperclip issue list --company acme
  paperclip --launcher-fetch-node -- run

Environment
  PAPERCLIP_NODE            Explicit Node binary to use.
  PAPERCLIP_PAYLOAD         Explicit payload root.
  PAPERCLIP_ENTRY           Explicit entry JS file.
  PAPERCLIP_DATA_DIR        Override the per-user data directory.
  PAPERCLIP_FETCH_NODE=1    Permit downloading a portable Node at runtime.
  PAPERCLIP_NODE_OPTIONS    Extra Node flags (space-separated) for the app.
  PAPERCLIP_LAUNCHER_DEBUG=1  Trace launcher decisions on stderr.
  PAPERCLIP_NODE_MIRROR     Mirror for Node downloads.
"""


def _debug(message: str) -> None:
    if env_flag("PAPERCLIP_LAUNCHER_DEBUG"):
        print(f"[paperclip-launcher] {message}", file=sys.stderr)


def split_launcher_flags(argv: Sequence[str]) -> Tuple[List[str], List[str], bool]:
    """Separate launcher flags from app arguments.

    Returns ``(launcher_flags, app_args, saw_double_dash)``. Parsing stops at the
    first bare ``--``; anything after it is app arguments even if it looks like a
    launcher flag (so you can never accidentally swallow a real argument).

    Launcher parsing also ends at the first token that is not a launcher flag:
    the app has its own ``--flags`` and they must never be inspected.
    """
    args = list(argv)
    launcher_flags: List[str] = []
    app_args: List[str] = []
    saw_double_dash = False
    index = 0

    while index < len(args):
        arg = args[index]
        if arg == "--":
            saw_double_dash = True
            index += 1
            break
        if arg.startswith(LAUNCHER_FLAG_PREFIX):
            launcher_flags.append(arg)
            index += 1
            # Consume a separate value for flags that take one, e.g.
            # `--launcher-node /opt/node/bin/node`.
            base = arg.split("=", 1)[0]
            if (
                base in LAUNCHER_FLAGS_WITH_VALUE
                and "=" not in arg
                and index < len(args)
                and args[index] != "--"
            ):
                launcher_flags.append(args[index])
                index += 1
            continue
        break

    app_args = args[index:]
    return launcher_flags, app_args, saw_double_dash


def _flag_value(flags: Sequence[str], name: str) -> Optional[str]:
    """Value for ``--launcher-foo=bar`` or ``--launcher-foo bar``."""
    prefix = f"{name}="
    for index, flag in enumerate(flags):
        if flag.startswith(prefix):
            return flag[len(prefix) :]
        if flag == name and index + 1 < len(flags):
            return flags[index + 1]
    return None


def _node_options_from_env() -> List[str]:
    raw = os.environ.get("PAPERCLIP_NODE_OPTIONS", "").strip()
    if not raw:
        return []
    return raw.split()


def _app_version(payload: Optional[Payload]) -> Optional[str]:
    if payload is None:
        return None
    return payload_manifest(payload).get("app_version")


def print_info(payload: Optional[Payload], runtime: Optional[NodeRuntime]) -> int:
    plat = current_platform()
    postgres = inspect_postgres(payload.node_modules if payload else None, plat)
    info = {
        "launcher_version": __version__,
        "platform": {"os": plat.os, "arch": plat.arch, "frozen": plat.is_frozen},
        "minimum_node_version": MINIMUM_NODE_VERSION,
        "node": runtime.describe() if runtime else None,
        "payload": payload_manifest(payload) if payload else None,
        "embedded_postgres": {
            "slug": postgres.slug,
            "present": postgres.present,
            "wrapper_present": postgres.wrapper_present,
            "siblings_ok": postgres.siblings_ok,
            "healthy": postgres.healthy,
            "missing_binaries": postgres.missing_binaries,
            "package_dir": str(postgres.package_dir) if postgres.package_dir else None,
            "lib_dir": str(postgres.lib_dir) if postgres.lib_dir else None,
            "notes": postgres.notes,
        },
        "data_dir": str(data_dir()),
        "bundled_root": str(bundled_root()),
        "app_root": str(app_root()),
    }
    json.dump(info, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def print_doctor(payload: Optional[Payload], runtime: Optional[NodeRuntime], errors: List[str]) -> int:
    plat = current_platform()
    print("Paperclip launcher doctor")
    print("=" * 60)
    print(f"  launcher version : {__version__}")
    print(f"  platform         : {plat.describe()}")
    print(f"  frozen           : {'yes' if plat.is_frozen else 'no (running from source)'}")
    print(f"  data dir         : {data_dir()}")
    print(f"  bundled root     : {bundled_root()}")
    print()

    print("Node runtime")
    if runtime is not None:
        print(f"  OK  {runtime.describe()}")
        if runtime.version and not runtime.version.satisfies_minimum():
            print(f"  !!  below required minimum {MINIMUM_NODE_VERSION}")
    else:
        print(f"  FAIL  no Node >= {MINIMUM_NODE_VERSION} found")
    print()

    print("Application payload")
    if payload is not None:
        print(f"  OK  entry        : {payload.entry}")
        print(f"      root         : {payload.root}")
        print(f"      node_modules : {payload.node_modules or 'MISSING'}")
        print(f"      discovered via: {payload.source}")
        version = _app_version(payload)
        print(f"      app version  : {version or 'unknown'}")
        if payload.node_modules is None:
            print("  !!  no node_modules beside the payload; external npm deps may fail to resolve")
    else:
        print("  FAIL  payload not found")
    print()

    print("Embedded PostgreSQL")
    postgres = inspect_postgres(payload.node_modules if payload else None, plat)
    for line in postgres_doctor_lines(postgres):
        print(line)
    print()

    if errors:
        print("Problems")
        for error in errors:
            print(f"  - {error}")
        print()

    # Node and the app bundle are mandatory; Postgres is not, because Paperclip
    # also runs against an external DATABASE_URL. Report it as a warning only.
    blocking = runtime is None or payload is None or (payload is not None and payload.node_modules is None)
    print("Result: " + ("not ready -- see failures above" if blocking else "ready to run"))
    if not blocking and not postgres.healthy and postgres.supported_platform:
        print("Note:   embedded Postgres is incomplete; set DATABASE_URL to use an external database")
    return LAUNCHER_ERROR_EXIT if blocking else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Launcher entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    launcher_flags, app_args, _ = split_launcher_flags(raw)

    # Double-click handling: when launched from Explorer with no args,
    # default to doctor so user sees something useful instead of hanging.
    # This is the fix for "double-click runs" requirement.
    # Env var allows overriding for testing.
    if not raw and not env_flag("PAPERCLIP_LAUNCHER_SHOW_HELP_ON_NO_ARGS"):
        # Check if we're in a double-click scenario (no args) - run doctor
        # Unless PAPERCLIP_LAUNCHER_NO_DEFAULT_DOCTOR is set
        if not env_flag("PAPERCLIP_LAUNCHER_NO_DEFAULT_DOCTOR"):
            raw = ["doctor"]
            launcher_flags, app_args, _ = split_launcher_flags(raw)

    if "--launcher-help" in launcher_flags or (
        not raw and env_flag("PAPERCLIP_LAUNCHER_SHOW_HELP_ON_NO_ARGS")
    ):
        sys.stdout.write(LAUNCHER_HELP)
        return 0

    # One-shot overrides take precedence over the environment.
    node_override = _flag_value(launcher_flags, "--launcher-node")
    if node_override:
        os.environ["PAPERCLIP_NODE"] = node_override
    payload_override = _flag_value(launcher_flags, "--launcher-payload")
    if payload_override:
        os.environ["PAPERCLIP_PAYLOAD"] = payload_override

    allow_fetch = "--launcher-fetch-node" in launcher_flags or env_flag("PAPERCLIP_FETCH_NODE")

    errors: List[str] = []

    payload: Optional[Payload] = None
    try:
        payload = find_payload()
        _debug(payload.describe())
    except PayloadError as exc:
        errors.append(str(exc))
        _debug(f"payload error: {exc}")

    runtime: Optional[NodeRuntime] = None
    try:
        runtime = resolve_node(allow_fetch=allow_fetch)
        _debug(runtime.describe())
    except NodeRuntimeError as exc:
        errors.append(str(exc))
        _debug(f"runtime error: {exc}")

    # --launcher-version is answered before the health gate: it is the one thing
    # you can always ask a broken install, and it must not require Node.
    if "--launcher-version" in launcher_flags:
        print(f"paperclip-launcher {__version__}")
        print(f"node {runtime.version} ({runtime.source})" if runtime else f"node NOT FOUND (need >= {MINIMUM_NODE_VERSION})")
        print(f"paperclip app {_app_version(payload) or ('unknown' if payload else 'PAYLOAD NOT FOUND')}")
        return 0

    if "--launcher-info" in launcher_flags:
        return print_info(payload, runtime)
    if "--launcher-doctor" in launcher_flags:
        return print_doctor(payload, runtime, errors)

    if runtime is None or payload is None:
        # Emit the actionable diagnostics, then a distinct non-zero exit so
        # scripts can tell "launcher misconfigured" from "app failed".
        for error in errors:
            print(error, file=sys.stderr)
        print(
            "\nRun 'paperclip --launcher-doctor' for a full diagnosis.",
            file=sys.stderr,
        )
        return LAUNCHER_ERROR_EXIT

    postgres = inspect_postgres(payload.node_modules, current_platform())
    if not postgres.healthy and postgres.supported_platform:
        _debug(f"embedded postgres incomplete: {postgres.describe()}")

    command = build_command(
        runtime.path,
        payload.entry,
        app_args,
        node_options=_node_options_from_env(),
    )
    env = build_environment(
        runtime.path,
        payload.node_modules,
        library_paths=linux_lib_path_entries(postgres),
        windows_paths=windows_path_entries(postgres),
        darwin_paths=darwin_path_entries(postgres),
    )
    _debug(f"exec: {' '.join(command)}")
    _debug(f"cwd : {payload.cwd}")
    return run_app(command, env, payload.cwd)


def cli() -> None:
    """Console-script wrapper: run :func:`main` and exit with its code."""
    try:
        code = main()
    except BrokenPipeError:
        # e.g. `paperclip --help | head` -- exit quietly like a well-behaved CLI.
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)
    except KeyboardInterrupt:
        os._exit(130)
    sys.exit(code)


if __name__ == "__main__":
    cli()
