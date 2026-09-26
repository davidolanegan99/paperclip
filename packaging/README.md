# Paperclip Packaging

This folder contains everything needed to build a **double-clickable .exe** for Paperclip on Windows.

## The Problem (Solved)

Original PyInstaller onefile build had 3 critical issues:

1. **Embedded Postgres broken**: pnpm nests `@embedded-postgres/<slug>` inside `embedded-postgres/node_modules/`, but app resolves as sibling. Staging didn't hoist, so Postgres failed offline.

2. **ESM externals missing**: CLI bundle is ESM with external deps (zod, commander, etc.). pnpm keeps transitive deps in `.pnpm` store, not flat. Node's ESM resolver couldn't find them, causing `ERR_MODULE_NOT_FOUND: Cannot find package 'zod'`.

3. **AV flagged**: PyInstaller onefile self-extracts to `%TEMP%` and runs from there - classic malware pattern. Unsigned binaries get quarantined. Nothing can fix this except signing + avoiding onefile, or using native launcher.

## The Solution

### 1. Fix Staging (build_exe.py)

- **Hoist embedded Postgres**: `_hoist_embedded_postgres_packages()` finds all `@embedded-postgres/*` in staged tree and pnpm store, copies to top-level `node_modules/@embedded-postgres/` sibling layout.

- **Hoist all transitive deps**: `_hoist_all_external_deps()` runs `pnpm list --parseable --depth=Infinity`, deduplicates by package name, copies missing packages to flat `node_modules/`. Fixes zod resolution.

- **Fix package.json location**: Copy to both `payload/app/package.json` and `payload/package.json` because `version.ts` does `require("../package.json")` from `app/index.js` which resolves to `payload/package.json`.

- **Default to onedir**: Changed `--onedir` to default True, added `--onefile` opt-in. Onedir has far fewer AV false positives than onefile.

- **Pre-create Linux .so aliases**: Embedded Postgres needs writable `native/lib` for `.so` aliases. Pre-create as real copies, not symlinks, so frozen bundle works.

### 2. Fix Launcher (launcher.py + Zig launcher)

**Python launcher (paperclip_exe/launcher.py):**

- Double-click handling: No args defaults to `doctor` (useful first-run)
- Windows PATH: Prepends `native/bin` and `native/lib` for Postgres DLLs
- Linux LD_LIBRARY_PATH: Prepends `native/lib` for .so
- macOS DYLD_LIBRARY_PATH: Same

**Zig launcher (windows-launcher/launcher.zig):**

- **Tiny native exe**: 202KB vs PyInstaller 150MB
- **No self-extraction**: Runs directly, no TEMP unpack
- **No Python runtime**: Pure native, no interpreter
- **Low AV false positives**: Zig binaries are rare in malware, not targeted by heuristics
- **Double-click**: No args -> `doctor`
- **Absolute path resolution**: Fixes relative PAYLOAD path bug
- **Postgres PATH**: Prepends native/bin and native/lib to PATH/LD_LIBRARY_PATH

### 3. Windows Installer (windows-installer/)

**Inno Setup installer (paperclip.iss):**

- Standard Windows installer (like VS Code, Chrome) - trusted by AV
- Does NOT self-extract to TEMP
- Installs to `%LOCALAPPDATA%\Paperclip` (no admin needed - reduces SmartScreen)
- Bundles portable Node.js >= 24.11.0, so the target machine needs no Node install
- Creates Start Menu and Desktop shortcuts
- Optionally adds to PATH
- Runs doctor after install
- Output: `Paperclip-Setup-0.3.1.exe` - far fewer AV flags than PyInstaller

**Zig installer (installer.zig):**

- Tiny portable installer (206KB) that copies payload
- Distribute as zip: `paperclip-installer.exe` + `payload/` + `paperclip.exe`
- User double-clicks installer, it sets up to `%LOCALAPPDATA%\Paperclip`

## Building

### Linux/macOS (for testing)

```bash
# Stage payload with fixes
python3 packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze

# Build Zig launchers (cross-compiles Windows exe from Linux)
./packaging/windows-launcher/build.sh

# Test
PAPERCLIP_ALLOW_OLD_NODE=1 PAPERCLIP_PAYLOAD=packaging/exe/build/payload ./packaging/exe/dist/paperclip --launcher-doctor
PAPERCLIP_ALLOW_OLD_NODE=1 PAPERCLIP_PAYLOAD=packaging/exe/build/payload ./packaging/exe/dist/paperclip doctor
PAPERCLIP_ALLOW_OLD_NODE=1 PAPERCLIP_PAYLOAD=packaging/exe/build/payload ./packaging/exe/dist/paperclip  # double-click test
```

### Windows (production)

```bat
REM Build the standalone onedir bundle and the single Setup.exe
powershell -ExecutionPolicy Bypass -File packaging\windows-installer\build-installer.ps1
REM Output: packaging\windows-installer\dist\Paperclip-Setup-0.3.1.exe
```

## Distribution Options

### Option 1: Inno Setup Installer (Recommended)

Single exe installer, standalone:

```
Paperclip-Setup-0.3.1.exe
```

The Setup.exe contains the complete onedir application, including a portable
Node runtime. The user experience is:

1. Double-click Setup.exe
2. Next, Next, Finish
3. Paperclip runs its launcher doctor
4. Use the Start Menu “Paperclip Onboard” shortcut for first-run setup

Build: `powershell -ExecutionPolicy Bypass -File packaging/windows-installer/build-installer.ps1`

### Option 2: Portable Zip (Zig launcher)

Tiny launcher + payload:

```
Paperclip-Portable-0.3.1.zip
  paperclip.exe (202KB)
  paperclip.bat (double-click wrapper)
  payload/ (staged app + node_modules + assets)
  runtime/ (portable Node.js runtime)
  README.txt
```

User:
1. Unzip
2. Double-click paperclip.exe or paperclip.bat
3. Runs doctor

Build:

```bash
python packaging/exe/build_exe.py --stage-payload --embed-node --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
./packaging/windows-launcher/build.sh
python - << 'PY'
import shutil, pathlib
dist = pathlib.Path("packaging/windows-installer/dist/Paperclip-Portable-0.3.1")
dist.mkdir(parents=True, exist_ok=True)
shutil.copy("packaging/exe/dist/paperclip.exe", dist / "paperclip.exe")
shutil.copy("packaging/windows-installer/paperclip.bat", dist / "paperclip.bat")
shutil.copytree("packaging/exe/build/payload", dist / "payload", dirs_exist_ok=True)
shutil.copytree("packaging/exe/build/runtime", dist / "runtime", dirs_exist_ok=True)
shutil.make_archive(str(dist), "zip", dist.parent, dist.name)
print(f"Created {dist}.zip")
PY
```

### Option 3: Python Onedir (Fallback)

If Zig not available, Python onedir is still better than onefile:

```bash
python packaging/exe/build_exe.py --stage-payload --freeze --onedir
```

Output: `packaging/exe/dist/paperclip/` folder (150MB). Ship as zip. Double-click `paperclip/paperclip.exe` runs doctor.

### Option 4: PyInstaller Onefile (Not Recommended)

High AV false positives, but single file:

```bash
python packaging/exe/build_exe.py --stage-payload --freeze --onefile
```

Output: `paperclip.exe` (150MB). Will be flagged by AV if unsigned.

## Verification

```bash
# 1. Staging fixes
ls packaging/exe/build/payload/node_modules | grep -E "zod|@embedded-postgres"  # should have zod and @embedded-postgres
ls packaging/exe/build/payload/node_modules/@embedded-postgres/  # should have linux-x64 or windows-x64
ls packaging/exe/build/payload/node_modules/@embedded-postgres/linux-x64/native/lib/  # should have .so aliases

# 2. Zig launcher
./packaging/exe/dist/paperclip --launcher-doctor  # should show postgres paths healthy
./packaging/exe/dist/paperclip doctor  # should run Paperclip doctor (banner + config check)
./packaging/exe/dist/paperclip  # no args -> should default to doctor (double-click)

# 3. Python launcher tests
python -m unittest discover -s packaging/exe/tests -t packaging/exe -v  # should be 167 OK

# 4. Payload entry
ls packaging/exe/build/payload/package.json packaging/exe/build/payload/app/package.json  # both should exist
```

## AV / SmartScreen Notes

Even with fixes, **unsigned** binaries will show SmartScreen "Unknown publisher" on first run. This is unavoidable without code signing cert.

Minimizing SmartScreen:

1. **Sign with EV cert** (best, $300-600/year): Reputation builds quickly
2. **Sign with OV cert**: Reputation builds over time
3. **Unsigned but low AV**: Zig + Inno Setup has far fewer flags than PyInstaller, but SmartScreen still shows "Unknown"
4. **Publish via GitHub Releases**: SmartScreen builds reputation via download count

Tell users: If SmartScreen appears, click "More info" -> "Run anyway" (common for new publishers).

## File Layout

```
packaging/
  exe/
    build_exe.py           # Main build script with staging fixes
    build-exe.sh/.bat      # One-click build
    paperclip_exe/
      launcher.py          # Python launcher with double-click + PATH fixes
      staging.py           # Staging with MAX_FILES 1M + .pnpm skip
      postgres.py          # Postgres PATH helpers
      runner.py            # Runner with windows_paths + darwin_paths
    dist/
      paperclip.exe        # Zig launcher Windows (202KB)
      paperclip            # Zig launcher Linux (57KB)
      paperclip-installer.exe  # Zig installer Windows (206KB)
    build/
      payload/             # Staged payload with flat node_modules
  windows-launcher/
    launcher.zig           # Zig launcher (low AV)
    installer.zig          # Zig installer
    build.sh/.bat          # Build Zig launchers
    README.md
  windows-installer/
    paperclip.iss          # Inno Setup script (low AV installer)
    paperclip.bat          # Double-click wrapper
    README.md
```

## Requirements

- **Build**: Python 3.9+, Node.js 24.11+, pnpm, PyInstaller, and Inno Setup 6 (Windows installer)
- **Runtime**: the Windows Setup.exe bundles Node.js 24.11+; target users do not install Node.js.

## License

MIT - Same as Paperclip
