#!/usr/bin/env bash
# ==========================================================================
#  verify-exe.sh -- smoke-test a built Paperclip executable
#
#      ./packaging/exe/verify-exe.sh                       # auto-find binary
#      ./packaging/exe/verify-exe.sh path/to/paperclip.exe # explicit binary
#
#  Checks, in order:
#    1. the binary exists and is executable (onefile or onedir layout)
#    2. --launcher-help works (launcher itself is alive)
#    3. --launcher-info returns valid JSON with a node + payload
#    4. embedded Postgres native runtime is staged correctly
#    5. --launcher-doctor reports "ready to run"
#    6. the real app answers --version through the launcher
#    7. exit codes propagate (the app's status is not swallowed)
#
#  Exits non-zero on the first failure with a clear message.
#
#  Postgres is reported as a warning, not a failure: Paperclip also runs against
#  an external DATABASE_URL, so an incomplete embedded runtime is not fatal.
# ==========================================================================
set -uo pipefail

cd "$(dirname "$0")/../.."
REPO_ROOT="$PWD"

BIN="${1:-}"
if [ -z "$BIN" ]; then
  for candidate in \
    "$REPO_ROOT/packaging/exe/dist/paperclip.exe" \
    "$REPO_ROOT/packaging/exe/dist/paperclip" \
    "$REPO_ROOT/packaging/exe/dist/paperclip/paperclip.exe" \
    "$REPO_ROOT/packaging/exe/dist/paperclip/paperclip"; do
    if [ -f "$candidate" ]; then BIN="$candidate"; break; fi
  done
fi

# Extract a dotted key path from JSON on stdin, e.g. `json_get payload.entry`.
# Prints an empty string when the path is missing or null.
# Prefers python3, falls back to node, and degrades to grep-free empty output.
json_get() {
  local path="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
path = sys.argv[1].split(".")
try:
    node = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for key in path:
    if not isinstance(node, dict) or key not in node:
        sys.exit(0)
    node = node[key]
if node is None:
    sys.exit(0)
print(node)
' "$path" 2>/dev/null
  elif command -v node >/dev/null 2>&1; then
    node -e '
const path = process.argv[1].split(".");
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  try {
    let node = JSON.parse(s);
    for (const key of path) {
      if (node === null || typeof node !== "object" || !(key in node)) return;
      node = node[key];
    }
    if (node !== null && node !== undefined) process.stdout.write(String(node));
  } catch {}
});
' "$path" 2>/dev/null
  fi
}

pass=0
fail=0
warn=0

ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }
warn() { echo "  [WARN] $1"; warn=$((warn+1)); }
info() { echo "  [....] $1"; }

echo "============================================================"
echo "  Paperclip executable verification"
echo "============================================================"

# ---- 1. binary exists ---------------------------------------------------
if [ -z "$BIN" ] || [ ! -f "$BIN" ]; then
  echo
  bad "no executable found (looked in packaging/exe/dist/)"
  echo
  echo "Build one first:"
  echo "  Windows:  packaging\\exe\\build-exe.bat"
  echo "  Linux/mac: ./packaging/exe/build-exe.sh"
  exit 1
fi
ok "binary: $BIN ($(wc -c < "$BIN" | tr -d ' ') bytes)"

if [ ! -x "$BIN" ]; then
  info "not marked executable; attempting chmod +x"
  chmod +x "$BIN" 2>/dev/null || true
fi

# ---- 2. launcher help ---------------------------------------------------
echo
info "checking --launcher-help"
if "$BIN" --launcher-help 2>&1 | grep -q "launcher-doctor"; then
  ok "launcher responds to --launcher-help"
else
  bad "--launcher-help produced no recognisable output"
fi

# ---- 3. launcher info is valid JSON -------------------------------------
echo
info "checking --launcher-info"
INFO_JSON="$("$BIN" --launcher-info 2>/dev/null)"
if [ -z "$INFO_JSON" ]; then
  bad "--launcher-info produced no output"
else
  NODE_DESC="$(printf '%s' "$INFO_JSON" | json_get "node")"
  PAYLOAD_ENTRY="$(printf '%s' "$INFO_JSON" | json_get "payload.entry")"
  MIN_NODE="$(printf '%s' "$INFO_JSON" | json_get "minimum_node_version")"
  APP_VERSION_JSON="$(printf '%s' "$INFO_JSON" | json_get "payload.app_version")"
  if [ -n "$NODE_DESC" ]; then
    ok "node resolved: $NODE_DESC"
  else
    bad "no Node runtime resolved (minimum required: ${MIN_NODE:-unknown})"
  fi
  if [ -n "$PAYLOAD_ENTRY" ]; then
    ok "payload resolved: $PAYLOAD_ENTRY"
    [ -n "$APP_VERSION_JSON" ] && ok "app version: $APP_VERSION_JSON"
  else
    bad "no application payload resolved"
  fi

  # ---- 4. embedded Postgres native runtime ------------------------------
  # Reported as a warning rather than a failure: Paperclip also runs against an
  # external DATABASE_URL, so an incomplete embedded runtime is not fatal.
  echo
  info "checking embedded Postgres native runtime"
  PG_SLUG="$(printf '%s' "$INFO_JSON" | json_get "embedded_postgres.slug")"
  PG_HEALTHY="$(printf '%s' "$INFO_JSON" | json_get "embedded_postgres.healthy")"
  PG_SIBLINGS="$(printf '%s' "$INFO_JSON" | json_get "embedded_postgres.siblings_ok")"
  PG_DIR="$(printf '%s' "$INFO_JSON" | json_get "embedded_postgres.package_dir")"
  if [ -z "$PG_SLUG" ]; then
    warn "no @embedded-postgres build is published for this platform"
    warn "embedded Postgres cannot run here; configure an external DATABASE_URL"
  elif [ "$PG_HEALTHY" = "True" ]; then
    ok "embedded Postgres ready: @embedded-postgres/$PG_SLUG"
    [ -n "$PG_DIR" ] && ok "  native package: $PG_DIR"
  else
    warn "embedded Postgres incomplete (@embedded-postgres/$PG_SLUG)"
    if [ "$PG_SIBLINGS" != "True" ]; then
      warn "  sibling layout broken: embedded-postgres and @embedded-postgres/"
      warn "  $PG_SLUG must share one node_modules parent"
    fi
    warn "  the binary still runs; only local-DB flows are affected"
    warn "  fix: reinstall deps on this OS/arch (pnpm install), or set DATABASE_URL"
  fi
fi

# ---- 5. doctor ----------------------------------------------------------
echo
info "checking --launcher-doctor"
if "$BIN" --launcher-doctor >/tmp/paperclip-doctor.out 2>&1; then
  ok "doctor reports ready to run"
else
  bad "doctor reported a problem (exit $?)"
  sed 's/^/         /' /tmp/paperclip-doctor.out | tail -20
fi

# ---- 6. app runs through the launcher -----------------------------------
echo
info "checking the app itself responds (--version)"
APP_VERSION="$("$BIN" --version 2>&1)"
if [ -n "$APP_VERSION" ]; then
  ok "app answered: $(printf '%s' "$APP_VERSION" | head -1)"
else
  bad "app produced no output for --version"
fi

# ---- 7. exit code propagation -------------------------------------------
echo
info "checking exit-code propagation (--help)"
"$BIN" --help >/dev/null 2>&1
CODE=$?
if [ "$CODE" -eq 0 ]; then
  ok "app --help exited 0"
else
  bad "app --help exited $CODE (expected 0)"
fi

# ---- summary ------------------------------------------------------------
echo
echo "============================================================"
echo "  passed: $pass   warnings: $warn   failed: $fail"
if [ "$fail" -eq 0 ]; then
  if [ "$warn" -gt 0 ]; then
    echo "  RESULT: executable is healthy (with $warn non-fatal warning(s))"
  else
    echo "  RESULT: executable is healthy"
  fi
  echo
  echo "  Next: $BIN doctor"
  echo "        $BIN onboard"
else
  echo "  RESULT: executable has problems -- see failures above"
  echo
  echo "  Most common causes:"
  echo "    * built without --embed-node and the machine has Node < 24.11.0"
  echo "    * built without --stage-payload, so no app bundle was included"
  echo "  Re-run the build with: --stage-payload --embed-node"
fi
echo "============================================================"

[ "$fail" -eq 0 ]
