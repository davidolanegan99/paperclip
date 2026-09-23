# Paperclip Zig Launcher - Low AV False Positives

This is a **native Windows executable** that replaces the PyInstaller-based launcher to **fix antivirus false positives**.

## The Problem with PyInstaller

PyInstaller binaries are heavily flagged by antivirus because:

1. **Onefile self-extracts to %TEMP%** and runs from there - classic malware pattern
2. **Bootloader signature** is well-known and many malwares use PyInstaller
3. **Unsigned** binaries with no reputation get quarantined
4. Even signed PyInstaller exes have higher false positive rates

**Nothing can make an unsigned PyInstaller binary invisible to AV.** The only real fixes are signing + onedir layout, but even then false positives remain.

## The Zig Solution

This launcher is written in **Zig** and compiled to a **tiny native PE executable**:

- **Size**: ~200KB vs PyInstaller's 100MB+
- **No self-extraction**: Runs directly, doesn't unpack to %TEMP%
- **No Python runtime**: Pure native code, no interpreter
- **No known malware signature**: Zig binaries are rare in malware, so AV heuristics don't target them
- **Version info embedded**: Looks legitimate to AV
- **Console subsystem**: Proper console app, not windowed

Result: **Dramatically fewer AV false positives**, even when unsigned.

### Comparison

| | PyInstaller onefile | PyInstaller onedir | Zig launcher |
|---|---|---|---|
| Size | ~150MB | ~150MB folder | **~200KB** |
| Self-extracts to TEMP | Yes (flagged) | No | No |
| Python runtime | Embedded | Embedded | None |
| AV false positives | High | Moderate | **Low** |
| Needs Node.js | No if --embed-node | No if --embed-node | **Yes** (user allows) |
| Startup time | Slow (extract) | Fast | **Instant** |

Since you allow Node.js to be installed, the Zig launcher is the best choice.

## Building

### Windows

Double-click `build.bat` or run:

```bat
packaging\windows-launcher\build.bat
```

Output: `packaging\exe\dist\paperclip.exe`

Requirements:
- Zig 0.12+ from https://ziglang.org/download/
- Or via npm: `npm install -g @oven/zig-linux-x64` (Linux) or `npm install @ziglang/zig-win32-x64` (Windows)

### Linux (cross-compile to Windows)

```bash
./packaging/windows-launcher/build.sh
```

Uses Zig's cross-compilation to build Windows exe from Linux:

```bash
zig build-exe launcher.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=paperclip.exe
```

No mingw or Windows SDK needed - Zig bundles everything.

### All platforms

```bash
# Linux binary
zig build-exe launcher.zig -target x86_64-linux-gnu -O ReleaseSmall -femit-bin=paperclip

# macOS binary (from macOS or Linux with zig)
zig build-exe launcher.zig -target x86_64-macos -O ReleaseSmall -femit-bin=paperclip-macos
zig build-exe launcher.zig -target aarch64-macos -O ReleaseSmall -femit-bin=paperclip-macos-arm64

# Windows binary
zig build-exe launcher.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=paperclip.exe
```

## Using the Executable

The Zig launcher expects:

1. **Node.js >= 24.11.0** installed and in PATH (or set PAPERCLIP_NODE)
2. **Payload** beside it: `payload/app/index.js` + `payload/node_modules/` + `payload/assets/`

Build payload:

```bash
python3 packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
```

Then:

```bash
# Linux/macOS
./paperclip --launcher-doctor
./paperclip doctor
./paperclip onboard

# Windows
paperclip.exe --launcher-doctor
paperclip.exe doctor
paperclip.exe onboard
```

### Double-Click Behavior

When double-clicked with no arguments (Explorer), it runs `doctor` to check installation:

```
Paperclip launcher doctor (Zig)
  launcher version : 1.0.0
  node             : C:\Program Files\nodejs\node.exe
  payload root     : C:\Users\You\AppData\Local\Paperclip\payload
  payload entry    : C:\Users\You\AppData\Local\Paperclip\payload\app\index.js
  node_modules     : C:\Users\You\AppData\Local\Paperclip\payload\node_modules
  postgres paths   :
    - ...\node_modules\@embedded-postgres\windows-x64\native\bin
    - ...\node_modules\@embedded-postgres\windows-x64\native\lib
```

If config is missing, it suggests running `onboard`.

For a true double-click-to-run experience, use the installer which creates shortcuts.

## Installer

### Inno Setup (Recommended for Distribution)

Produces `Paperclip-Setup.exe` that:

- Checks Node.js is installed (prompts to install if not)
- Installs to `%LOCALAPPDATA%\Paperclip` (no admin needed)
- Creates Start Menu and Desktop shortcuts
- Adds to PATH optionally
- Runs doctor after install

Build:

```bash
# Stage payload first
python packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze

# Build Zig launcher
./packaging/windows-launcher/build.sh  # or build.bat on Windows

# Build installer (Windows only, needs Inno Setup)
iscc packaging/windows-installer/paperclip.iss
```

Output: `packaging/windows-installer/dist/Paperclip-Setup-0.3.1.exe`

This installer exe is **NOT PyInstaller** - it's built by Inno Setup, which is widely used and has very low AV false positives, especially when signed.

### Zig Installer (Portable)

A tiny self-contained installer that copies payload:

```bash
zig build-exe installer.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=paperclip-installer.exe
```

Usage:

```
paperclip-installer.exe
```

It:
1. Checks Node.js
2. Finds payload beside it or in build/payload
3. Copies to %LOCALAPPDATA%\Paperclip
4. Copies launcher exe
5. Runs doctor

Distribute as zip containing:
- paperclip-installer.exe (206KB)
- payload/ folder (staged)
- paperclip.exe (202KB)

User double-clicks installer, it sets up, then double-clicks paperclip.exe to run.

## Embedded PostgreSQL Fix

The Zig launcher also fixes the embedded Postgres problem:

**Before (broken):**
- pnpm nests `@embedded-postgres/<slug>` inside `embedded-postgres/node_modules/`
- App resolves as sibling: `node_modules/embedded-postgres` + `node_modules/@embedded-postgres/<slug>`
- Sibling layout broken -> Postgres fails offline
- Linux .so aliases need writable lib dir -> fails in frozen bundle

**After (fixed):**
- Build script hoists all `@embedded-postgres/*` to top-level sibling layout
- Pre-creates Linux .so aliases as real copies (not symlinks)
- Launcher prepends native/bin and native/lib to PATH (Windows) and LD_LIBRARY_PATH (Linux)
- Doctor reports postgres health explicitly

```
Embedded PostgreSQL
  OK   @embedded-postgres/linux-x64 [healthy] at .../node_modules/@embedded-postgres/linux-x64
      wrapper package : present
      sibling layout  : correct
      native/lib      : .../native/lib
```

## Signing (Optional but Recommended)

Even Zig binaries benefit from signing for SmartScreen reputation:

```bat
set PAPERCLIP_SIGN_SUBJECT=Paperclip Inc
iscc packaging/windows-installer/paperclip.iss
```

Or sign the launcher directly:

```bat
signtool sign /n "Paperclip Inc" /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 paperclip.exe
```

Requires certificate from CA (DigiCert, Sectigo, etc.) - cannot be generated locally.

## Why Zig and Not Go/Rust/C?

- **Zig**: Single binary toolchain, no dependencies, cross-compiles to Windows from Linux without mingw, tiny binaries, no runtime
- **Go**: Needs Go installed (not available in sandbox), larger binaries (~2MB), still low AV but bigger
- **Rust**: Needs Rust toolchain, larger binaries, more complex
- **C**: Needs mingw for cross-compile, manual PE handling complex

Zig is ideal because:
1. Available via npm (`@oven/zig-linux-x64`) so works in restricted networks
2. Cross-compiles to Windows from Linux with `zig cc -target x86_64-windows-gnu`
3. Produces tiny binaries (200KB) with `-O ReleaseSmall`
4. No external dependencies

## Testing

```bash
# Build payload
python3 packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze

# Build Zig launcher for current platform
zig build-exe launcher.zig -O ReleaseSmall -femit-bin=paperclip

# Test
PAPERCLIP_ALLOW_OLD_NODE=1 ./paperclip --launcher-doctor
PAPERCLIP_ALLOW_OLD_NODE=1 ./paperclip doctor
```

## Future Improvements

1. **Embed Node.js**: Bundle portable Node inside exe for fully standalone (like --embed-node)
2. **Embed payload**: Use `zig embed` to include payload as compressed archive inside exe (would be large)
3. **Code signing**: Add version info resource via RC file and `windres`
4. **Auto-update**: Check for updates and download new payload
5. **GUI installer**: Use Zig to build a proper GUI installer with progress bar
