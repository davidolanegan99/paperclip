"""Spawning the real Paperclip app and propagating its behaviour faithfully.

The single most important property of the launcher is *transparency*: an
interactive CLI (``@clack/prompts``), raw-mode TUIs, piped stdin, ``Ctrl+C`` and
exit codes must behave exactly as they do when running ``node cli/dist/index.js``
directly. That is achieved by:

* inheriting stdio (no pipes) so the child owns the console and TTY modes work;
* not creating a new process group, so ``Ctrl+C`` reaches the child directly;
* forwarding termination signals to the child and mirroring its exit status,
  including the "killed by signal N" convention (exit code 128 + N).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from . import __version__ as __launcher_version__
from ._platform import LINUX, MACOS, WINDOWS, current_platform

#: Exit code used when the launcher itself fails before the app could start.
LAUNCHER_ERROR_EXIT = 78  # EX_CONFIG (sysexits.h)


def build_environment(
    node_path: Path,
    payload_node_modules: Optional[Path],
    extra: Optional[Mapping[str, str]] = None,
    library_paths: Sequence[str] = (),
) -> Dict[str, str]:
    """Environment for the child process.

    * Inherits ours verbatim (the app reads a lot of ``PAPERCLIP_*`` config).
    * Puts the resolved Node's directory first on ``PATH`` so anything the app
      shells out to (npm, npx, corepack) finds the same runtime we validated.
    * Adds ``NODE_PATH`` for the payload's ``node_modules`` as a resolver
      backstop for externals loaded from unusual depths.
    * Prepends ``library_paths`` to ``LD_LIBRARY_PATH`` so the embedded Postgres
      native libraries resolve even for subprocesses spawned before the app's
      own ``prepareEmbeddedPostgresNativeRuntime()`` hook runs.
    * Marks the process as running under the packaged launcher.
    """
    env: Dict[str, str] = dict(os.environ)

    node_dir = str(node_path.parent)
    path_sep = os.pathsep
    existing_path = env.get("PATH", "")
    parts = [p for p in existing_path.split(path_sep) if p]
    if node_dir not in parts:
        parts.insert(0, node_dir)
    env["PATH"] = path_sep.join(parts)

    if payload_node_modules is not None:
        existing_node_path = env.get("NODE_PATH", "")
        candidates = [str(payload_node_modules)]
        candidates += [p for p in existing_node_path.split(path_sep) if p]
        env["NODE_PATH"] = path_sep.join(dict.fromkeys(candidates))

    if library_paths:
        existing = env.get("LD_LIBRARY_PATH", "")
        entries = list(library_paths)
        entries += [p for p in existing.split(path_sep) if p]
        env["LD_LIBRARY_PATH"] = path_sep.join(dict.fromkeys(entries))

    env["PAPERCLIP_EXE_LAUNCHER"] = "1"
    env.setdefault("PAPERCLIP_LAUNCHER_VERSION", __launcher_version__)
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def build_command(
    node_path: Path,
    entry: Path,
    app_args: Sequence[str],
    node_options: Sequence[str] = (),
) -> List[str]:
    """Full argv for the child process.

    ``node_options`` are inserted before the entry point. We deliberately do not
    inject ``--experimental-*`` flags by default: the app must run with the same
    semantics as the npm install.
    """
    command: List[str] = [str(node_path)]
    command.extend(node_options)
    command.append(str(entry))
    command.extend(app_args)
    return command


def _forward_signal(proc: "subprocess.Popen[bytes]", signum: int) -> None:
    """Best-effort forwarding of a signal to the child."""
    try:
        proc.send_signal(signum)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Child already gone, or signal not deliverable -- nothing to do.
        pass


def _install_forwarders(proc: "subprocess.Popen[bytes]") -> List[int]:
    """Forward termination signals so the app can shut down cleanly.

    ``SIGINT`` is intentionally *not* forwarded on POSIX: the child shares our
    process group and already receives ``Ctrl+C`` from the terminal. Forwarding
    it again would deliver a duplicate interrupt.
    """
    installed: List[int] = []
    candidates = [signal.SIGTERM, signal.SIGBREAK if hasattr(signal, "SIGBREAK") else None]
    if current_platform().os == WINDOWS:
        # On Windows Ctrl+C only reaches the foreground process, so forward it.
        candidates.append(signal.SIGINT)
    for sig in candidates:
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda _num, _frame, p=proc, s=sig: _forward_signal(p, s))
            installed.append(sig)
        except (OSError, ValueError, RuntimeError):
            # Cannot install (not main thread, unsupported signal) -- skip.
            continue
    return installed


def _restore_forwarders(installed: Sequence[int]) -> None:
    for sig in installed:
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError, RuntimeError):
            continue


def spawn(command: Sequence[str], env: Mapping[str, str], cwd: Path) -> "subprocess.Popen[bytes]":
    """Start the child with fully inherited stdio."""
    kwargs: Dict[str, object] = {
        "stdin": None,
        "stdout": None,
        "stderr": None,
        "env": dict(env),
        "cwd": str(cwd),
        "close_fds": True,
    }
    if current_platform().os == WINDOWS:
        # Do NOT detach or create a new console: the child must share ours so
        # interactive prompts and raw-mode TUIs keep working.
        kwargs["creationflags"] = 0
    else:
        # Share our process group so terminal signals reach the child.
        kwargs["start_new_session"] = False
    return subprocess.Popen(list(command), **kwargs)  # noqa: S603 (trusted argv)


def exit_code_from_returncode(returncode: int) -> int:
    """Translate a Popen returncode into a POSIX-style exit code.

    Negative returncodes mean "killed by signal N" on POSIX; convention is to
    exit with ``128 + N`` so shells and CI see the expected status.
    """
    if returncode < 0:
        return 128 + (-returncode)
    return returncode


def run_app(
    command: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
) -> int:
    """Run the app to completion and return the exit code to propagate."""
    try:
        proc = spawn(command, env, cwd)
    except FileNotFoundError as exc:
        print(f"paperclip: failed to start {command[0]}: {exc}", file=sys.stderr)
        return LAUNCHER_ERROR_EXIT
    except OSError as exc:
        print(f"paperclip: failed to start the application: {exc}", file=sys.stderr)
        return LAUNCHER_ERROR_EXIT

    installed = _install_forwarders(proc)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # Give the child a chance to clean up, then mirror the interrupt.
        _forward_signal(proc, signal.SIGINT)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            returncode = proc.wait()
    finally:
        _restore_forwarders(installed)
    return exit_code_from_returncode(returncode)
