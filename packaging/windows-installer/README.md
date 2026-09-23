# Paperclip Windows Installer

This folder contains the **Inno Setup** installer for Paperclip that fixes AV flagging issues.

## Why Inno Setup and Not PyInstaller?

PyInstaller binaries are heavily flagged because they self-extract to `%TEMP%` and run from there - a classic malware pattern.

Inno Setup installers are:
- Standard Windows installers (like VS Code, Chrome, etc.)
- Do NOT self-extract to TEMP
- Widely used and trusted by AV vendors
- Tiny (5-10MB vs PyInstaller's 150MB)
- Properly versioned and can be signed

Even unsigned, Inno Setup has **far fewer false positives** than PyInstaller.

## Building the Installer

### 1. Stage Payload

```bash
python packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
```

This creates `packaging/exe/build/payload/` with:
- `app/index.js` (bundled CLI)
- `app/package.json`
- `package.json` (for version.ts require)
- `node_modules/` (flat, includes zod + all 700+ deps + @embedded-postgres sibling layout)
- `assets/` (migrations, skills, etc.)

The staging fixes two critical bugs:
1. **Embedded Postgres**: Hoists `@embedded-postgres/*` from pnpm store to sibling layout so offline Postgres works
2. **ESM externals**: Flattens all transitive deps (zod, commander, etc.) so `import zod` works

### 2. Build Zig Launcher

Windows:
```bat
packaging\windows-launcher\build.bat
```

Linux (cross-compile):
```bash
./packaging/windows-launcher/build.sh
```

Output: `packaging/exe/dist/paperclip.exe` (202KB)

This is a **native** exe, NOT PyInstaller. Double-click runs doctor.

### 3. Build Inno Setup Installer

Requires Inno Setup 6 from https://jrsoftware.org/isinfo.php

```bat
iscc packaging\windows-installer\paperclip.iss
```

Output: `packaging\windows-installer\dist\Paperclip-Setup-0.3.1.exe`

### 4. (Optional) Sign

```bat
set PAPERCLIP_SIGN_SUBJECT=Your Company Name
iscc packaging/windows-installer/paperclip.iss
```

Or manually:

```bat
signtool sign /n "Your Company" /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 dist\Paperclip-Setup-0.3.1.exe
```

## What the Installer Does

1. Checks Node.js >= 24.11.0 is in PATH
   - If not, opens https://nodejs.org/ and aborts
2. Installs to `%LOCALAPPDATA%\Paperclip` (no admin needed - reduces SmartScreen)
3. Copies `payload/` and `paperclip.exe`
4. Creates Start Menu shortcut: `Paperclip` -> `paperclip.exe doctor`
5. Creates optional Desktop shortcut
6. Optionally adds to PATH
7. Runs `paperclip doctor` after install

User experience:
- Download `Paperclip-Setup-0.3.1.exe`
- Double-click
- Next, Next, Finish
- Paperclip runs, shows banner, runs doctor
- If no config, suggests `paperclip onboard`

## Portable Distribution

For users who prefer portable (no installer):

Create zip containing:
- `paperclip.exe` (Zig launcher, 202KB)
- `payload/` folder (staged)
- `paperclip.bat` (double-click wrapper)
- `README.txt`

Build:

```bash
python packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
./packaging/windows-launcher/build.sh
python - << 'PY'
import shutil, pathlib
dist = pathlib.Path("packaging/windows-installer/dist/Paperclip-Portable-0.3.1")
dist.mkdir(parents=True, exist_ok=True)
shutil.copy("packaging/exe/dist/paperclip.exe", dist / "paperclip.exe")
shutil.copy("packaging/windows-installer/paperclip.bat", dist / "paperclip.bat")
shutil.copytree("packaging/exe/build/payload", dist / "payload", dirs_exist_ok=True)
shutil.make_archive(str(dist), "zip", dist.parent, dist.name)
print(f"Created {dist}.zip")
PY
```

User:
- Unzip
- Double-click `paperclip.exe` or `paperclip.bat`
- Runs doctor

## Double-Click Behavior

The Zig launcher detects double-click (no args) and defaults to `doctor`:

```zig
if (args.len == 0) {
    // No args -> double-clicked in Explorer
    // Default to doctor for useful first-run experience
    args = .{ "doctor" };
}
```

This means:
- Double-click `paperclip.exe` -> runs doctor, shows banner, checks health
- Command line `paperclip.exe onboard` -> runs onboard
- Command line `paperclip.exe --help` -> shows help

The batch wrapper `paperclip.bat` keeps console open after double-click:

```bat
@echo off
if "%~1"=="" (
  paperclip.exe doctor
  pause
) else (
  paperclip.exe %*
)
```

## SmartScreen and AV

Even with Inno Setup, **unsigned** binaries will show SmartScreen "Unknown publisher" on first run. This is unavoidable without an EV code signing certificate ($300-600/year).

To minimize SmartScreen:

1. **Sign with EV cert** (best): SmartScreen reputation builds quickly
2. **Sign with OV cert**: Reputation builds over time as more users install
3. **Unsigned but low AV**: Zig launcher + Inno Setup has far fewer AV flags than PyInstaller, but SmartScreen still shows "Unknown"

The Python launcher (onedir) is also low AV vs onefile, but still higher than Zig because it embeds Python.

**Recommendation for distribution:**
- Use Zig launcher + Inno Setup (current)
- Sign installer with certificate if you have one
- Publish via GitHub Releases so SmartScreen can build reputation via download count
- In README, tell users to click "More info" -> "Run anyway" if SmartScreen appears (common for new publishers)

## Testing the Installer

On Windows VM:

```bat
# Build
python packaging\exe\build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
packaging\windows-launcher\build.bat
iscc packaging\windows-installer\paperclip.iss

# Test
dist\Paperclip-Setup-0.3.1.exe
# Should:
# - Check Node
# - Install to %LOCALAPPDATA%\Paperclip
# - Create shortcuts
# - Run doctor

# Test double-click
%LOCALAPPDATA%\Paperclip\paperclip.exe
# Should run doctor, show banner

# Test PATH
paperclip doctor
# Should work if "Add to PATH" was selected
```

On Linux (simulate):

```bash
PAPERCLIP_ALLOW_OLD_NODE=1 PAPERCLIP_PAYLOAD=packaging/exe/build/payload packaging/exe/dist/paperclip doctor
```

## Files

- `paperclip.iss`: Inno Setup script
- `paperclip.bat`: Double-click wrapper batch file
- `README.md`: This file
- `dist/`: Output folder (gitignored)

## License

MIT - Same as Paperclip
