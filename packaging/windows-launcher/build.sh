#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO_ROOT="$PWD"
echo "Building Zig launchers..."

# Find zig
ZIG=""
if command -v zig >/dev/null 2>&1; then
  ZIG=zig
elif [ -x "/usr/local/lib/node_modules/@oven/zig-linux-x64/zig" ]; then
  ZIG="/usr/local/lib/node_modules/@oven/zig-linux-x64/zig"
elif [ -x "./node_modules/.bin/zig" ]; then
  ZIG="./node_modules/.bin/zig"
elif [ -x "/usr/local/bin/zig" ]; then
  ZIG="/usr/local/bin/zig"
fi

if [ -z "$ZIG" ]; then
  echo "Zig not found. Installing via npm..."
  npm install -g @oven/zig-linux-x64 2>&1 || true
  if [ -x "/usr/local/lib/node_modules/@oven/zig-linux-x64/zig" ]; then
    ZIG="/usr/local/lib/node_modules/@oven/zig-linux-x64/zig"
  elif command -v zig >/dev/null 2>&1; then
    ZIG=zig
  else
    echo "Failed to find zig at /usr/local/lib/node_modules/@oven/zig-linux-x64/zig"
    ls /usr/local/lib/node_modules/@oven/ 2>&1 || true
    exit 1
  fi
fi

echo "Using: $($ZIG version 2>&1 || echo zig) at $ZIG"

mkdir -p packaging/exe/dist

echo "[..] Building paperclip (linux x64)..."
$ZIG build-exe packaging/windows-launcher/launcher.zig -target x86_64-linux-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip 2>&1 | head -n 20

echo "[..] Building paperclip.exe (windows x64)..."
$ZIG build-exe packaging/windows-launcher/launcher.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip.exe 2>&1 | head -n 20

echo "[..] Building paperclip-installer (linux x64)..."
$ZIG build-exe packaging/windows-launcher/installer.zig -target x86_64-linux-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip-installer 2>&1 | head -n 20

echo "[..] Building paperclip-installer.exe (windows x64)..."
$ZIG build-exe packaging/windows-launcher/installer.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip-installer.exe 2>&1 | head -n 20

echo ""
echo "Built:"
ls -lh packaging/exe/dist/paperclip* 2>&1
echo ""
echo "Test:"
PAPERCLIP_ALLOW_OLD_NODE=1 PAPERCLIP_PAYLOAD=packaging/exe/build/payload ./packaging/exe/dist/paperclip --launcher-doctor 2>&1 | head -n 20 || true
