"""Python launcher + build pipeline for the Paperclip single-file executable.

The Paperclip application itself is a Node.js/TypeScript codebase (server ~970k
LOC, React UI, Rust runner, 210 DB tables). It cannot be rewritten in Python, so
instead this package produces a genuine distributable ``paperclip.exe`` that:

* carries the *real* application payload (the same esbuild bundle npm ships),
* carries or locates a Node.js runtime >= 24.11.0,
* execs it with inherited stdio, so interactive prompts, TUIs, TTY raw mode,
  signals and exit codes behave exactly like running ``paperclipai`` directly.

Because the payload is the untouched upstream bundle, behaviour is identical --
there is no reimplementation to drift.

Modules
-------
runtime     Node.js discovery, version parsing, portable-runtime download.
payload     Locating the application bundle + its node_modules root.
runner      Process spawning, exit-code/signal propagation.
launcher    CLI entry point used by the frozen executable.
build_exe   Host-side build orchestrator (produces the .exe).
"""

from __future__ import annotations

__all__ = ["__version__", "MINIMUM_NODE_VERSION"]

#: Version of the packaging pipeline itself (independent of the app version).
__version__ = "1.0.0"

#: Mirrors ``MINIMUM_NODE_VERSION`` in ``packages/shared/src/node-version.ts``.
#: Keep in sync -- ``build_exe.py --check`` verifies this against the repo.
MINIMUM_NODE_VERSION = "24.11.0"
