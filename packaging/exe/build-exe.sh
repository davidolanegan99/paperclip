#!/usr/bin/env bash
# ==========================================================================
#  build-exe.sh -- one-click Linux/macOS build of the Paperclip binary
#
#      ./packaging/exe/build-exe.sh
#
#  Produces packaging/exe/dist/paperclip (a single self-contained file).
#  A Windows .exe can only be produced on Windows -- use build-exe.bat there,
#  or let the GitHub Actions workflow build it for you.
#
#  Requirements: Python 3.9+, Node.js 24.11+ (build time AND run time, since
#  the binary uses the Node already installed), pnpm.
#
#  Extra arguments are forwarded to build_exe.py, e.g.:
#      ./packaging/exe/build-exe.sh --onedir
#      ./packaging/exe/build-exe.sh --embed-node
# ==========================================================================
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO_ROOT="$PWD"

echo
echo "============================================================"
echo "  Paperclip executable builder"
echo "  repo: $REPO_ROOT"
echo "============================================================"
echo

fail() { echo "[FAIL] $*" >&2; exit 1; }

# ---- locate Python ------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  fail "Python 3 was not found on PATH."
fi
echo "[OK  ] using: $PY ($($PY --version 2>&1))"

# ---- locate Node --------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "[WARN] Node.js was not found on PATH."
  echo "       The built binary runs the app with the machine's Node, so"
  echo "       Node 24.11.0+ must be installed at run time (or build with"
  echo "       --embed-node to bundle a portable runtime)."
else
  echo "[OK  ] node: $(node --version)"
fi

# ---- virtualenv (avoids PEP 668 "externally-managed-environment") -------
VENV_DIR="${PAPERCLIP_EXE_VENV:-$REPO_ROOT/packaging/exe/.venv-build}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[..  ] creating build virtualenv at $VENV_DIR"
  "$PY" -m venv "$VENV_DIR" || fail "could not create a virtualenv (is python3-venv installed?)"
fi
VENV_PY="$VENV_DIR/bin/python"

# ---- ensure PyInstaller -------------------------------------------------
if ! "$VENV_PY" -m PyInstaller --version >/dev/null 2>&1; then
  echo "[..  ] installing PyInstaller into the build virtualenv"
  "$VENV_PY" -m pip install --quiet --upgrade pip pyinstaller
fi
echo "[OK  ] PyInstaller $("$VENV_PY" -m PyInstaller --version 2>&1)"

# ---- warn about shared libpython (the main Linux packaging pitfall) -----
if [ "$(uname -s)" = "Linux" ]; then
  if ! "$VENV_PY" - <<'PYCHECK' >/dev/null 2>&1
import os, sys, sysconfig
libdir = sysconfig.get_config_var("LIBDIR") or ""
ldlib = sysconfig.get_config_var("LDLIBRARY") or ""
candidate = os.path.join(libdir, ldlib)
sys.exit(0 if ldlib and os.path.exists(candidate) else 1)
PYCHECK
  then
    PYVER="$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "[WARN] This Python has no shared libpython, which PyInstaller requires."
    echo "       (Typical of Debian/Ubuntu *system* Python.) Fix with one of:"
    echo "         sudo apt install libpython${PYVER}"
    echo "       or install Python from https://www.python.org/downloads/"
    echo "       or: uv python install 3.12 && uv venv --python 3.12"
    echo "       Windows and macOS are unaffected."
  fi
fi

# ---- install workspace deps if needed -----------------------------------
if [ ! -d "$REPO_ROOT/node_modules" ] && [ ! -d "$REPO_ROOT/cli/node_modules" ]; then
  echo "[..  ] no node_modules found -- running pnpm install"
  if ! command -v pnpm >/dev/null 2>&1; then
    echo "[..  ] pnpm not found; enabling corepack"
    corepack enable || true
  fi
  pnpm install --frozen-lockfile || fail "pnpm install failed"
fi

# ---- run the build ------------------------------------------------------
echo
"$VENV_PY" packaging/exe/build_exe.py --stage-payload --freeze --run-doctor "$@"

echo
echo "============================================================"
if [ -f "$REPO_ROOT/packaging/exe/dist/paperclip" ]; then
  echo "  DONE  packaging/exe/dist/paperclip"
  echo
  echo "  Try it:"
  echo "    packaging/exe/dist/paperclip --launcher-doctor"
  echo "    packaging/exe/dist/paperclip doctor"
elif [ -f "$REPO_ROOT/packaging/exe/dist/paperclip/paperclip" ]; then
  echo "  DONE  packaging/exe/dist/paperclip/  (onedir layout)"
  echo
  echo "  Ship the whole folder (or tar/zip it). Run:"
  echo "    packaging/exe/dist/paperclip/paperclip --launcher-doctor"
else
  echo "  Build finished but dist/paperclip was not found."
fi
echo "============================================================"
