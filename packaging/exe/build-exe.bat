@echo off
REM ==========================================================================
REM  build-exe.bat -- one-click Windows build of paperclip.exe
REM
REM  Double-click this file, or run it from a terminal:
REM      packaging\exe\build-exe.bat
REM
REM  Requirements (all free):
REM    * Python 3.9+   https://www.python.org/downloads/windows/
REM                    IMPORTANT: tick "Add python.exe to PATH" in the installer.
REM                    python.org builds are recommended -- they ship python3xx.dll,
REM                    which PyInstaller needs (Windows Store Python often does not).
REM    * Node.js 24.11+ https://nodejs.org/  -- required at BUILD time only
REM                    (the produced distribution embeds its own Node runtime).
REM    * pnpm           corepack enable      (ships with Node)
REM
REM  Output: a standalone bundle in packaging\exe\dist\paperclip\
REM          The end-user does not need Node.js installed.
REM
REM  Extra arguments are forwarded to build_exe.py, e.g.:
REM      packaging\exe\build-exe.bat --onefile
REM      packaging\exe\build-exe.bat --sign
REM ==========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0..\.."

echo.
echo ============================================================
echo   Paperclip .exe builder
echo   repo: %CD%
echo ============================================================
echo.

REM ---- locate Python -------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [FAIL] Python was not found on PATH.
    echo        Install it from https://www.python.org/downloads/windows/
    echo        and tick "Add python.exe to PATH" during setup.
    goto :fail
)
echo [OK  ] using: %PY%
%PY% --version

REM ---- locate Node and check its version -----------------------------------
where node >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Node.js was not found on PATH.
    echo        Install Node 24.11.0 or newer from https://nodejs.org/
    echo        The built exe runs the app with this Node installation.
    goto :fail
)
for /f "delims=" %%v in ('node --version') do set "NODE_VER=%%v"
echo [OK  ] node: %NODE_VER%

REM ---- ensure PyInstaller --------------------------------------------------
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [..  ] installing PyInstaller...
    %PY% -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [..  ] retrying with --user
        %PY% -m pip install --user --upgrade pyinstaller
    )
    %PY% -m PyInstaller --version >nul 2>nul
    if errorlevel 1 (
        echo [FAIL] could not install PyInstaller.
        echo        Try:  %PY% -m pip install --user pyinstaller
        goto :fail
    )
)
for /f "delims=" %%v in ('%PY% -m PyInstaller --version') do echo [OK  ] PyInstaller %%v

REM ---- install workspace deps if needed ------------------------------------
if not exist "node_modules" if not exist "cli\node_modules" (
    echo [..  ] no node_modules found -- running pnpm install
    where pnpm >nul 2>nul
    if errorlevel 1 (
        echo [..  ] pnpm not found; enabling corepack
        corepack enable
    )
    call pnpm install --frozen-lockfile
    if errorlevel 1 (
        echo [FAIL] pnpm install failed. Fix the errors above and re-run.
        goto :fail
    )
)

REM ---- run the build -------------------------------------------------------
echo.
%PY% packaging\exe\build_exe.py --stage-payload --freeze --onedir --embed-node --run-doctor %*
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   DONE
echo.
if exist "packaging\exe\dist\paperclip.exe" (
    echo   packaging\exe\dist\paperclip.exe
    echo.
    echo   Try it:
    echo     packaging\exe\dist\paperclip.exe --launcher-doctor
    echo     packaging\exe\dist\paperclip.exe doctor
    echo.
    echo   This bundle includes Node.js; the target machine needs no Node install.
    echo   For a single self-extracting executable, add: --onefile
) else if exist "packaging\exe\dist\paperclip\paperclip.exe" (
    echo   packaging\exe\dist\paperclip\  (onedir layout)
    echo.
    echo   Ship the whole folder, or zip it. Run:
    echo     packaging\exe\dist\paperclip\paperclip.exe --launcher-doctor
) else (
    echo   Build finished but no paperclip.exe was found in dist\.
    echo   Re-run to see the full log.
)
echo ============================================================
goto :eof

:fail
echo.
echo Build failed. See the messages above.
exit /b 1
