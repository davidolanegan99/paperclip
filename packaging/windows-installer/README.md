# Paperclip Windows Setup

`Paperclip-Setup-0.3.1.exe` is the user-facing Windows distribution. It is a
single downloadable Inno Setup executable. After installation, Paperclip runs
from its installed folder with a portable Node.js runtime bundled inside the
application.

## End-user requirements

- Windows 10/11, x64
- No Node.js
- No npm, pnpm, Python, or developer tools

The installer puts Paperclip in `%LOCALAPPDATA%\Paperclip`, creates a Start
Menu shortcut, optionally creates a Desktop shortcut, and can add the Paperclip
command to the current user's PATH. The first launch runs `paperclip doctor`.

## Build the installer

Builds require Node.js, pnpm, Python, PyInstaller, and Inno Setup. Those are
**build-machine prerequisites only**; they are not copied into the user's
prerequisite list and Node.js is embedded in the resulting app.

On Windows PowerShell:

```powershell
# Installs/builds the standalone onedir bundle and creates Setup.exe
powershell -ExecutionPolicy Bypass -File packaging\windows-installer\build-installer.ps1
```

The script:

1. builds the real Paperclip CLI and payload;
2. downloads the official Node.js 24.11.0 Windows runtime;
3. embeds that runtime in the Paperclip onedir bundle;
4. verifies `paperclip.exe` and `_internal\runtime\node\node.exe` exist;
5. compresses the whole bundle into `dist\Paperclip-Setup-0.3.1.exe`.

To compile an already-built bundle:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows-installer\build-installer.ps1 -SkipExecutableBuild
```

The standalone build can also be triggered from the **Build executable** GitHub
Actions workflow. Windows builds always embed Node and upload the Setup.exe
artifact.

## What is installed

The single Setup.exe contains an onedir layout similar to:

```text
%LOCALAPPDATA%\Paperclip\
├── paperclip.exe
└── _internal\
    ├── payload\             # Paperclip CLI, server assets, npm dependencies
    └── runtime\node\
        └── node.exe         # portable Node.js; no system Node is consulted
```

The application launcher checks the bundled runtime before PATH. A machine's
Node.js installation is neither required nor used by the normal installed app.

## Security and SmartScreen

Inno Setup installs files normally instead of making the application unpack
itself into `%TEMP%` on every launch. The Windows artifacts should still be
Authenticode-signed for a production release. An unsigned first release can
show the normal SmartScreen “Unknown publisher” warning; signing and publishing
from a stable release channel builds reputation over time.

## Manual Inno Setup command

After running the build script, the script file can be compiled directly:

```powershell
iscc packaging\windows-installer\paperclip.iss
```

Do not use the old payload-only workflow for this Setup.exe. A valid installer
must be built from `packaging\exe\dist\paperclip\`, and that directory must
contain `_internal\runtime\node\node.exe`.

## Testing the installed app

On a Windows test machine with Node.js absent:

```text
1. Double-click Paperclip-Setup-0.3.1.exe.
2. Finish the wizard.
3. Leave “Launch Paperclip” enabled.
4. Confirm the launcher doctor reports a bundled Node runtime.
5. Use the Start Menu “Paperclip Onboard” shortcut for first-run setup.
```

The setup artifact is ignored by Git because it is a reproducible release
output. Upload it as a GitHub Actions artifact or attach it to a release.
